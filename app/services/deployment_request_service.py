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
from app.repositories.service_repository import ServiceRepository
from app.repositories.service_variable_repository import ServiceVariableRepository
from app.schemas.job import BuildJobPayload
from app.services.deployment_status_service import DeploymentStatusService
from app.services.scaling_config import ScalingConfig

logger = logging.getLogger(__name__)


class DeploymentRequestService:
    """배포 요청과 첫 job 을 함께 만든다. 트랜잭션 커밋은 호출하는 쪽이 한다.

    소스를 빌드하는 요청은 BUILD job, 이미 빌드한 이미지를 다시 쓰는 요청(롤백·재시작)은
    빌드를 건너뛰고 DEPLOY job, 서비스를 내리는 요청은 REMOVE job 으로 시작한다.

    환경변수는 요청 시점의 서비스 변수를 `variables_snapshot`(키 → 암호문)으로 저장한다.
    롤백만 원본 요청의 스냅샷을 쓴다. 재배포·재시작은 지금 변수를 써서, 변수를 고친 뒤
    다시 띄우면 고친 값이 반영된다.

    Pod 수와 리소스는 롤백을 포함해 모든 요청에서 지금 서비스의 원하는 설정을 고정한다.
    """

    def __init__(
        self,
        deployment_request_repository: DeploymentRequestRepository,
        job_repository: JobRepository,
        deployment_status_history_repository: DeploymentStatusHistoryRepository,
        build_repository: BuildRepository,
        service_variable_repository: ServiceVariableRepository,
        service_repository: ServiceRepository,
    ) -> None:
        self._deployment_request_repository = deployment_request_repository
        self._job_repository = job_repository
        self._deployment_status_history_repository = deployment_status_history_repository
        self._build_repository = build_repository
        self._service_variable_repository = service_variable_repository
        self._service_repository = service_repository

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
            variables_snapshot=None,
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

        소스 정보는 원본에서 가져온다. 환경변수는 롤백이면 원본의 스냅샷을, 재시작이면 지금
        변수를 쓴다. 요청은 QUEUED 를 거쳐 곧바로 DEPLOYING 이 된다.
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
            variables_snapshot=(
                source_deployment_request.variables_snapshot
                if trigger_type == DeploymentTrigger.ROLLBACK
                else None
            ),
        )
        if request is None:
            return None
        build = await self._build_repository.add(Build.copy_succeeded(source_build, request.id))
        await self._start_deploying(request)
        await self._job_repository.save(
            Job(
                deployment_request_id=request.id,
                kind=JobKind.DEPLOY,
                payload={"build_id": build.id},
            )
        )
        return request

    async def create_removal_request(
        self,
        service: Service,
        *,
        source_deployment_request: DeploymentRequest,
        idempotency_key: str,
        requested_by: int | None = None,
    ) -> DeploymentRequest | None:
        """지금 떠 있는 배포(원본)를 클러스터에서 내리는 요청과 REMOVE job 을 만든다.

        빌드·release 를 만들지 않는다. 요청은 QUEUED 를 거쳐 곧바로 DEPLOYING 이 되고,
        Deploy Worker 가 GitOps 에서 서비스 디렉터리를 지운 뒤 Argo CD Application 이
        사라지면 SUCCEEDED 로 끝낸다.
        멱등성 키가 겹치거나 이 서비스에 진행 중인 배포가 있으면 만들지 않고 None 이다.
        """
        request = await self._add_request(
            service,
            source_sha=source_deployment_request.source_sha,
            source_commit_message=source_deployment_request.source_commit_message,
            trigger_type=DeploymentTrigger.REMOVE,
            idempotency_key=idempotency_key,
            requested_by=requested_by,
            source_deployment_request=source_deployment_request,
            variables_snapshot=source_deployment_request.variables_snapshot,
        )
        if request is None:
            return None
        await self._start_deploying(request)
        await self._job_repository.save(Job(deployment_request_id=request.id, kind=JobKind.REMOVE))
        return request

    async def _start_deploying(self, request: DeploymentRequest) -> None:
        """빌드를 건너뛰는 요청을 QUEUED 에서 곧바로 DEPLOYING 으로 옮긴다."""
        await DeploymentStatusService(
            self._deployment_request_repository, self._deployment_status_history_repository
        ).transition_status(request.id, DeploymentStatus.DEPLOYING)

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
        variables_snapshot: dict[str, str] | None,
    ) -> DeploymentRequest | None:
        """`variables_snapshot` 가 None 이면 지금 서비스 변수를 스냅샷으로 저장한다."""
        if variables_snapshot is None:
            variables = await self._service_variable_repository.search_by_service_id(service.id)
            variables_snapshot = {v.key: v.encrypted_value for v in variables}
        scaling_config = await self._service_repository.get_scaling_config_for_update(service.id)
        scaling_snapshot = ScalingConfig.model_validate(
            scaling_config or ScalingConfig.defaults().model_dump(mode="json")
        ).model_dump(mode="json")
        request = await self._deployment_request_repository.add_if_absent(
            DeploymentRequest(
                service_id=service.id,
                environment=Environment.PROD,
                source_sha=source_sha,
                source_commit_message=source_commit_message,
                trigger_type=trigger_type,
                idempotency_key=idempotency_key,
                requested_by=requested_by,
                variables_snapshot=variables_snapshot,
                scaling_snapshot=scaling_snapshot,
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
