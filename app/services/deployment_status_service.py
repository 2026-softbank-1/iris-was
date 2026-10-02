import logging
from collections.abc import Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import InvalidInputError, InvalidStatusTransitionError
from app.enums import DeploymentStatus, FailureCode
from app.models.deployment_request import DeploymentRequest
from app.models.deployment_status_history import DeploymentStatusHistory
from app.repositories.deployment_request_repository import DeploymentRequestRepository
from app.repositories.deployment_status_history_repository import (
    DeploymentStatusHistoryRepository,
)

logger = logging.getLogger(__name__)

# 같은 서비스·환경에서 진행 중인 배포는 하나뿐이다. 진행 중 상태(QUEUED·BUILDING·DEPLOYING)끼리는
# 앞으로만 움직이고, 끝난 상태로만 빠져나간다.
ALLOWED_TRANSITIONS: Mapping[DeploymentStatus, frozenset[DeploymentStatus]] = {
    DeploymentStatus.QUEUED: frozenset({DeploymentStatus.BUILDING, DeploymentStatus.FAILED}),
    DeploymentStatus.BUILDING: frozenset({DeploymentStatus.DEPLOYING, DeploymentStatus.FAILED}),
    DeploymentStatus.DEPLOYING: frozenset(
        {
            DeploymentStatus.SUCCEEDED,
            DeploymentStatus.FAILED,
            DeploymentStatus.ROLLED_BACK,
            DeploymentStatus.MANUAL_INTERVENTION,
        }
    ),
    # 실패를 확인한 뒤에 되돌리거나 운영자에게 넘길 수 있다.
    DeploymentStatus.FAILED: frozenset(
        {DeploymentStatus.ROLLED_BACK, DeploymentStatus.MANUAL_INTERVENTION}
    ),
    DeploymentStatus.SUCCEEDED: frozenset(),
    DeploymentStatus.ROLLED_BACK: frozenset(),
    DeploymentStatus.MANUAL_INTERVENTION: frozenset(),
}


class DeploymentStatusService:
    """배포 요청의 `status` 를 바꾸는 유일한 경로. API 와 Worker 가 함께 쓴다.

    `status` 를 직접 UPDATE 하지 않고 이 서비스를 거치면 허용된 전이만 일어나고,
    전이마다 이력이 한 줄 남는다. 커밋은 호출하는 쪽이 한다.
    """

    def __init__(
        self,
        deployment_request_repository: DeploymentRequestRepository,
        deployment_status_history_repository: DeploymentStatusHistoryRepository,
        session: AsyncSession | None = None,
    ) -> None:
        self._deployment_request_repository = deployment_request_repository
        self._deployment_status_history_repository = deployment_status_history_repository
        self._session = session

    async def transition_status(
        self,
        deployment_request_id: int,
        to_status: DeploymentStatus,
        *,
        failure_code: FailureCode | None = None,
    ) -> DeploymentRequest:
        """배포 요청을 `to_status` 로 옮기고 이력을 남긴다.

        이미 `to_status` 이면 아무것도 하지 않는다(at-least-once 로 다시 처리돼도 안전하다).
        `FAILED` 로 갈 때만 `failure_code` 를 받고, 이때는 반드시 있어야 한다.
        """
        request = await self._deployment_request_repository.get_by_id_for_update(
            deployment_request_id
        )
        from_status = request.status
        if from_status == to_status:
            return request

        if to_status not in ALLOWED_TRANSITIONS[from_status]:
            raise InvalidStatusTransitionError(
                "status transition is not allowed",
                deployment_request_id=deployment_request_id,
                from_status=from_status,
                to_status=to_status,
            )
        if to_status == DeploymentStatus.FAILED and failure_code is None:
            raise InvalidInputError(
                "failure code is required when the deployment fails",
                deployment_request_id=deployment_request_id,
            )
        if to_status != DeploymentStatus.FAILED and failure_code is not None:
            raise InvalidInputError(
                "failure code is only allowed when the deployment fails",
                deployment_request_id=deployment_request_id,
                to_status=to_status,
            )

        request.transition_to(to_status, failure_code)
        await self._deployment_status_history_repository.add(
            DeploymentStatusHistory(
                deployment_request_id=request.id,
                from_status=from_status,
                to_status=to_status,
                failure_code=failure_code,
            )
        )
        if to_status == DeploymentStatus.FAILED and self._session is not None:
            from app.services.diagnosis_service import enqueue_deployment_diagnosis

            # Keep failure state authoritative even when a diagnostic helper is unavailable.
            try:
                async with self._session.begin_nested():
                    await enqueue_deployment_diagnosis(self._session, request)
            except Exception:
                logger.error(
                    "diagnosis admission failed",
                    extra={
                        "action": "enqueue_diagnosis",
                        "deployment_request_id": request.id,
                    },
                )
        logger.info(
            "deployment status transitioned",
            extra={
                "action": "transition_status",
                "deployment_request_id": request.id,
                "from_status": from_status,
                "to_status": to_status,
            },
        )
        return request
