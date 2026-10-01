import logging

from app.enums import DeploymentStatus, DeploymentTrigger, Environment, JobKind
from app.models.deployment_request import DeploymentRequest
from app.models.deployment_status_history import DeploymentStatusHistory
from app.models.job import Job
from app.models.service import Service
from app.repositories.deployment_request_repository import DeploymentRequestRepository
from app.repositories.deployment_status_history_repository import (
    DeploymentStatusHistoryRepository,
)
from app.repositories.job_repository import JobRepository
from app.schemas.job import BuildJobPayload

logger = logging.getLogger(__name__)


class DeploymentRequestService:
    """배포 요청과 첫 BUILD job 을 함께 만든다. 트랜잭션 커밋은 호출하는 쪽이 한다."""

    def __init__(
        self,
        deployment_request_repository: DeploymentRequestRepository,
        job_repository: JobRepository,
        deployment_status_history_repository: DeploymentStatusHistoryRepository,
    ) -> None:
        self._deployment_request_repository = deployment_request_repository
        self._job_repository = job_repository
        self._deployment_status_history_repository = deployment_status_history_repository

    async def create_deployment_request(
        self,
        service: Service,
        *,
        source_sha: str,
        source_commit_message: str | None,
        trigger_type: DeploymentTrigger,
        idempotency_key: str,
        requested_by: int | None = None,
    ) -> DeploymentRequest | None:
        """멱등성 키가 겹치거나 이 서비스에 진행 중인 배포가 있으면 만들지 않고 None 이다."""
        request = await self._deployment_request_repository.add_if_absent(
            DeploymentRequest(
                service_id=service.id,
                environment=Environment.PROD,
                source_sha=source_sha,
                source_commit_message=source_commit_message,
                trigger_type=trigger_type,
                idempotency_key=idempotency_key,
                requested_by=requested_by,
            )
        )
        if request is None:
            logger.info(
                "deployment request skipped",
                extra={
                    "action": "create_deployment_request",
                    "service_id": service.id,
                    "trigger_type": trigger_type,
                },
            )
            return None

        # 첫 이력. 대기 시간(QUEUED)이 언제부터인지 이 행으로 안다.
        await self._deployment_status_history_repository.add(
            DeploymentStatusHistory(
                deployment_request_id=request.id,
                from_status=None,
                to_status=DeploymentStatus.QUEUED,
            )
        )
        payload = BuildJobPayload(
            source_repository_url=service.source_repository_url,
            source_branch=service.source_branch,
            source_sha=source_sha,
            root_directory=service.root_directory,
        )
        await self._job_repository.save(
            Job(
                deployment_request_id=request.id,
                kind=JobKind.BUILD,
                payload=payload.model_dump(mode="json"),
            )
        )
        return request
