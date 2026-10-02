import logging

from app.enums import DeploymentStatus, DeploymentTrigger, Environment, JobKind
from app.models.build import Build
from app.models.deployment_request import DeploymentRequest
from app.models.deployment_status_history import DeploymentStatusHistory
from app.models.job import Job
from app.models.service import Service
from app.repositories.build_repository import BuildRepository
from app.repositories.deployment_request_repository import DeploymentRequestRepository
from app.repositories.deployment_status_history_repository import (
    DeploymentStatusHistoryRepository,
)
from app.repositories.job_repository import JobRepository
from app.schemas.job import BuildJobPayload
from app.services.deployment_status_service import DeploymentStatusService

logger = logging.getLogger(__name__)


class DeploymentRequestService:
    """배포 요청과 첫 job 을 함께 만든다. 트랜잭션 커밋은 호출하는 쪽이 한다.

    소스를 빌드하는 요청은 BUILD job, 이미 빌드한 이미지를 다시 쓰는 요청(롤백·재시작)은
    빌드를 건너뛰고 DEPLOY job 으로 시작한다.
    """

    def __init__(
        self,
        deployment_request_repository: DeploymentRequestRepository,
        job_repository: JobRepository,
        deployment_status_history_repository: DeploymentStatusHistoryRepository,
        build_repository: BuildRepository,
    ) -> None:
        self._deployment_request_repository = deployment_request_repository
        self._job_repository = job_repository
        self._deployment_status_history_repository = deployment_status_history_repository
        self._build_repository = build_repository

    async def create_deployment_request(
        self,
        service: Service,
        *,
        source_sha: str,
        source_commit_message: str | None,
        trigger_type: DeploymentTrigger,
        idempotency_key: str,
        requested_by: int | None = None,
        source_deployment_request: DeploymentRequest | None = None,
    ) -> DeploymentRequest | None:
        """멱등성 키가 겹치거나 이 서비스에 진행 중인 배포가 있으면 만들지 않고 None 이다.

        `source_deployment_request` 는 같은 소스로 다시 배포하는 요청(REDEPLOY)의 원본이다.
        """
        request = await self._add_request(
            service,
            source_sha=source_sha,
            source_commit_message=source_commit_message,
            trigger_type=trigger_type,
            idempotency_key=idempotency_key,
            requested_by=requested_by,
            source_deployment_request=source_deployment_request,
        )
        if request is None:
            return None
        build = await self._build_repository.add(Build(deployment_request_id=request.id))
        await self._job_repository.save(
            Job(
                deployment_request_id=request.id,
                kind=JobKind.BUILD,
                payload=BuildJobPayload(build_id=build.id).model_dump(mode="json"),
            )
        )
        return request

    async def create_deployment_request_reusing_image(
        self,
        service: Service,
        *,
        source_deployment_request: DeploymentRequest,
        source_build: Build,
        trigger_type: DeploymentTrigger,
        idempotency_key: str,
        requested_by: int | None = None,
    ) -> DeploymentRequest | None:
        """원본 요청이 만든 이미지로 빌드 없이 배포하는 요청과 DEPLOY job 을 만든다.

        소스 정보·환경변수는 원본에서 가져온다. 요청은 QUEUED 를 거쳐 곧바로 DEPLOYING 이 된다.
        멱등성 키가 겹치거나 이 서비스에 진행 중인 배포가 있으면 만들지 않고 None 이다.
        """
        request = await self._add_request(
            service,
            source_sha=source_deployment_request.source_sha,
            source_commit_message=source_deployment_request.source_commit_message,
            trigger_type=trigger_type,
            idempotency_key=idempotency_key,
            requested_by=requested_by,
            source_deployment_request=source_deployment_request,
        )
        if request is None:
            return None
        build = await self._build_repository.add(Build.copy_succeeded(source_build, request.id))
        await DeploymentStatusService(
            self._deployment_request_repository, self._deployment_status_history_repository
        ).transition_status(request.id, DeploymentStatus.DEPLOYING)
        await self._job_repository.save(
            Job(
                deployment_request_id=request.id,
                kind=JobKind.DEPLOY,
                payload={"build_id": build.id},
            )
        )
        return request

    async def _add_request(
        self,
        service: Service,
        *,
        source_sha: str,
        source_commit_message: str | None,
        trigger_type: DeploymentTrigger,
        idempotency_key: str,
        requested_by: int | None,
        source_deployment_request: DeploymentRequest | None,
    ) -> DeploymentRequest | None:
        request = await self._deployment_request_repository.add_if_absent(
            DeploymentRequest(
                service_id=service.id,
                environment=Environment.PROD,
                source_sha=source_sha,
                source_commit_message=source_commit_message,
                trigger_type=trigger_type,
                idempotency_key=idempotency_key,
                requested_by=requested_by,
                variables_snapshot=(
                    source_deployment_request.variables_snapshot
                    if source_deployment_request is not None
                    else None
                ),
                source_deployment_request_id=(
                    source_deployment_request.id if source_deployment_request is not None else None
                ),
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
        return request
