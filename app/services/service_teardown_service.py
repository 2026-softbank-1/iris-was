import logging
import uuid

from app.core.exceptions import DeploymentInProgressError
from app.enums import ACTIVE_DEPLOYMENT_STATUSES, DeploymentStatus, DeploymentTrigger
from app.models.service import Service
from app.repositories.deployment_request_repository import DeploymentRequestRepository
from app.services.deployment_request_service import DeploymentRequestService

logger = logging.getLogger(__name__)


class ServiceTeardownService:
    """서비스를 지울 때 떠 있는 앱도 내리는 REMOVE 요청을 만든다. 커밋은 호출하는 쪽이 한다.

    배포한 적이 있는 서비스마다 REMOVE 를 요청한다. 첫 배포가 실패해 GitOps 디렉터리와
    Application 이 남은 서비스도 같은 방식으로 정리된다. 디렉터리가 이미 없으면 Worker 가
    커밋 없이 끝낸다.
    """

    def __init__(
        self,
        deployment_request_repository: DeploymentRequestRepository,
        deployment_request_service: DeploymentRequestService,
    ) -> None:
        self._deployment_request_repository = deployment_request_repository
        self._deployment_request_service = deployment_request_service

    async def request_teardown(self, services: list[Service], requested_by: int) -> int:
        """만든 REMOVE 요청 수를 돌려준다.

        배포한 적이 없거나 이미 내려간 서비스는 건너뛴다. 진행 중인 배포가 있는 서비스가 하나라도
        있으면 아무것도 만들지 않고 DeploymentInProgressError 다.
        """
        if not services:
            return 0
        latest_by_service = await self._deployment_request_repository.search_latest_by_service_ids(
            [service.id for service in services]
        )
        targets = []
        for service in services:
            latest = latest_by_service.get(service.id)
            if latest is None:
                continue
            if latest.status in ACTIVE_DEPLOYMENT_STATUSES:
                raise DeploymentInProgressError(
                    "a deployment is in progress", service_id=service.id
                )
            is_already_removed = (
                latest.trigger_type == DeploymentTrigger.REMOVE
                and latest.status == DeploymentStatus.SUCCEEDED
            )
            if not is_already_removed:
                targets.append((service, latest))

        for service, latest in targets:
            request = await self._deployment_request_service.create_removal_request(
                service,
                source_deployment_request=latest,
                # 삭제를 다시 시도할 수 있게 요청마다 새 키를 쓴다. 중복은 진행 중 제약이 막는다.
                idempotency_key=f"delete:{service.id}:{uuid.uuid4()}",
                requested_by=requested_by,
            )
            if request is None:
                raise DeploymentInProgressError(
                    "a deployment is in progress", service_id=service.id
                )
            logger.info(
                "service teardown requested",
                extra={
                    "action": "request_teardown",
                    "service_id": service.id,
                    "deployment_request_id": request.id,
                },
            )
        return len(targets)
