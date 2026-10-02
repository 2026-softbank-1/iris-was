import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    DeploymentInProgressError,
    DeploymentRequestNotFoundError,
    InvalidInputError,
    PipelineRequiredError,
    ServiceNotFoundError,
)
from app.enums import DeploymentStatus, DeploymentTrigger
from app.models.deployment_request import DeploymentRequest
from app.models.service import Service
from app.repositories.deployment_request_repository import DeploymentRequestRepository
from app.repositories.service_repository import ServiceRepository
from app.services.deployment_request_service import DeploymentRequestService
from app.services.repository_url import parse_repository_url
from app.services.source_repository_service import SourceRepositoryService

logger = logging.getLogger(__name__)

_SOURCE_COPYING_TRIGGERS = frozenset({DeploymentTrigger.REDEPLOY, DeploymentTrigger.ROLLBACK})
_MANUAL_TRIGGERS = frozenset({DeploymentTrigger.MANUAL}) | _SOURCE_COPYING_TRIGGERS


class ManualDeploymentService:
    """사용자가 직접 만드는 배포 요청: 첫 배포·재배포·롤백. 웹훅 경로와 같은 생성 로직을 쓴다."""

    def __init__(
        self,
        session: AsyncSession,
        service_repository: ServiceRepository,
        deployment_request_repository: DeploymentRequestRepository,
        deployment_request_service: DeploymentRequestService,
        source_repository_service: SourceRepositoryService,
    ) -> None:
        self._session = session
        self._service_repository = service_repository
        self._deployment_request_repository = deployment_request_repository
        self._deployment_request_service = deployment_request_service
        self._source_repository_service = source_repository_service

    async def create_deployment_request(
        self,
        owner_id: int,
        service_id: int,
        *,
        trigger_type: DeploymentTrigger,
        source_sha: str | None = None,
        source_deployment_request_id: int | None = None,
        idempotency_key: str | None = None,
    ) -> DeploymentRequest:
        """같은 `idempotency_key` 로 다시 요청하면 처음 만든 배포 요청을 그대로 돌려준다."""
        if trigger_type not in _MANUAL_TRIGGERS:
            raise InvalidInputError("trigger type is not allowed here", trigger_type=trigger_type)
        service = await self._get_owned(owner_id, service_id)
        if (service.analysis_plan or {}).get("pipelineManaged"):
            raise PipelineRequiredError("start an analyzed pipeline before building or deploying")

        if trigger_type in _SOURCE_COPYING_TRIGGERS:
            commit_sha, commit_message = await self._copy_source(
                service, trigger_type, source_deployment_request_id
            )
        else:
            commit_sha, commit_message = await self._resolve_head(owner_id, service, source_sha)

        # 전역으로 유일한 키라서 서비스 id 를 붙여 다른 서비스의 키와 섞이지 않게 한다.
        key = f"manual:{service.id}:{idempotency_key or uuid.uuid4()}"
        request = await self._deployment_request_service.create_deployment_request(
            service,
            source_sha=commit_sha,
            source_commit_message=commit_message,
            trigger_type=trigger_type,
            idempotency_key=key,
            requested_by=owner_id,
        )
        if request is None:
            replayed = await self._deployment_request_repository.find_by_idempotency_key(key)
            if replayed is None:
                raise DeploymentInProgressError(
                    "a deployment is already in progress", service_id=service.id
                )
            return replayed

        await self._session.commit()
        logger.info(
            "manual deployment requested",
            extra={
                "action": "create_deployment_request",
                "service_id": service.id,
                "deployment_request_id": request.id,
                "trigger_type": trigger_type,
            },
        )
        return request

    async def _copy_source(
        self,
        service: Service,
        trigger_type: DeploymentTrigger,
        source_deployment_request_id: int | None,
    ) -> tuple[str, str | None]:
        if source_deployment_request_id is None:
            raise InvalidInputError(
                "source deployment is required",
                field="sourceDeploymentId",
                trigger_type=trigger_type,
            )
        source = await self._deployment_request_repository.find_by_id_and_service_id(
            source_deployment_request_id, service.id
        )
        if source is None:
            raise DeploymentRequestNotFoundError(
                "source deployment not found",
                service_id=service.id,
                deployment_request_id=source_deployment_request_id,
            )
        if (
            trigger_type == DeploymentTrigger.ROLLBACK
            and source.status != DeploymentStatus.SUCCEEDED
        ):
            raise InvalidInputError(
                "rollback target must be a succeeded deployment",
                field="sourceDeploymentId",
                deployment_request_id=source.id,
                status=source.status,
            )
        return source.source_sha, source.source_commit_message

    async def _resolve_head(
        self, owner_id: int, service: Service, source_sha: str | None
    ) -> tuple[str, str | None]:
        """`source_sha` 가 없으면 서비스 브랜치의 최신 커밋을 GitHub 에서 읽는다."""
        if source_sha is not None:
            return source_sha, None
        owner, name = parse_repository_url(service.source_repository_url)
        head = await self._source_repository_service.find_branch_head(
            owner_id, f"{owner}/{name}", service.source_branch
        )
        if head is None:
            raise InvalidInputError(
                "branch not found in repository", field="branch", branch=service.source_branch
            )
        return head.sha, head.message

    async def _get_owned(self, owner_id: int, service_id: int) -> Service:
        service = await self._service_repository.find_by_id_and_owner_id(service_id, owner_id)
        if service is None:
            raise ServiceNotFoundError("service not found", service_id=service_id)
        return service
