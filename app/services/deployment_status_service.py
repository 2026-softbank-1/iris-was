import logging
from collections.abc import Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import InvalidInputError, InvalidStatusTransitionError
from app.enums import ACTIVE_DEPLOYMENT_STATUSES, DeploymentStatus, FailureCode
from app.models.deployment_request import DeploymentRequest
from app.models.deployment_status_history import DeploymentStatusHistory
from app.repositories.deployment_request_repository import DeploymentRequestRepository
from app.repositories.deployment_status_history_repository import (
    DeploymentStatusHistoryRepository,
)
from app.services.stack_progress import StackDeploymentProgress

logger = logging.getLogger(__name__)

# 같은 서비스·환경에서 진행 중인 배포는 하나뿐이다. 진행 중 상태(QUEUED·BUILDING·DEPLOYING)끼리는
# 앞으로만 움직이고, 끝난 상태로만 빠져나간다. QUEUED → DEPLOYING 은 이미지를 다시 빌드하지 않는
# 요청(롤백·재시작)이 빌드를 건너뛸 때 쓴다.
ALLOWED_TRANSITIONS: Mapping[DeploymentStatus, frozenset[DeploymentStatus]] = {
    DeploymentStatus.QUEUED: frozenset(
        {
            DeploymentStatus.BUILDING,
            DeploymentStatus.DEPLOYING,
            DeploymentStatus.FAILED,
            DeploymentStatus.SUPERSEDED,
        }
    ),
    DeploymentStatus.BUILDING: frozenset(
        {DeploymentStatus.DEPLOYING, DeploymentStatus.FAILED, DeploymentStatus.SUPERSEDED}
    ),
    DeploymentStatus.DEPLOYING: frozenset(
        {
            DeploymentStatus.SUCCEEDED,
            DeploymentStatus.FAILED,
            DeploymentStatus.ROLLED_BACK,
            DeploymentStatus.MANUAL_INTERVENTION,
            DeploymentStatus.SUPERSEDED,
        }
    ),
    # 실패를 확인한 뒤에 되돌리거나 운영자에게 넘길 수 있다.
    DeploymentStatus.FAILED: frozenset(
        {DeploymentStatus.ROLLED_BACK, DeploymentStatus.MANUAL_INTERVENTION}
    ),
    DeploymentStatus.SUCCEEDED: frozenset(),
    DeploymentStatus.ROLLED_BACK: frozenset(),
    DeploymentStatus.MANUAL_INTERVENTION: frozenset(),
    DeploymentStatus.SUPERSEDED: frozenset(),
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
        stack_progress: StackDeploymentProgress | None = None,
    ) -> None:
        self._deployment_request_repository = deployment_request_repository
        self._deployment_status_history_repository = deployment_status_history_repository
        # 스택 배포의 다음 단계 진행(시작·보류). 끝난 상태로 옮길 때만 쓴다.
        self._stack_progress = stack_progress

    @classmethod
    def create(cls, session: AsyncSession) -> "DeploymentStatusService":
        """같은 세션(=같은 트랜잭션)에서 상태를 옮기려는 Worker 용."""
        return cls(
            DeploymentRequestRepository(session),
            DeploymentStatusHistoryRepository(session),
            StackDeploymentProgress(session),
        )

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
        logger.info(
            "deployment status transitioned",
            extra={
                "action": "transition_status",
                "deployment_request_id": request.id,
                "from_status": from_status,
                "to_status": to_status,
            },
        )
        if self._stack_progress is not None and to_status not in ACTIVE_DEPLOYMENT_STATUSES:
            await self._stack_progress.on_request_finished(request, self)
        return request
