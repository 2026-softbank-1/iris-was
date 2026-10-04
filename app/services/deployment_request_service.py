import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.config import DEFAULT_ONPREM_SERVER_OFFLINE_AFTER_SECONDS
from app.core.exceptions import TargetNotConnectedError
from app.enums import (
    BuildStatus,
    DeploymentStatus,
    DeploymentStrategy,
    DeploymentTrigger,
    Environment,
    JobKind,
    OnpremServerConnectionStatus,
)
from app.models.build import Build
from app.models.deployment_request import DeploymentRequest
from app.models.deployment_status_history import DeploymentStatusHistory
from app.models.job import Job
from app.models.service import Service
from app.models.service_variable import ServiceVariable
from app.repositories.build_repository import BuildRepository
from app.repositories.deployment_request_repository import DeploymentRequestRepository
from app.repositories.deployment_status_history_repository import (
    DeploymentStatusHistoryRepository,
)
from app.repositories.job_repository import JobRepository
from app.repositories.service_repository import ServiceRepository
from app.repositories.service_variable_repository import ServiceVariableRepository
from app.schemas.job import BuildJobPayload
from app.services.database_engines import ImageRef
from app.services.deployment_status_service import DeploymentStatusService
from app.services.deployment_strategy import resolve_deployment_strategy
from app.services.scaling_config import ScalingConfig

logger = logging.getLogger(__name__)


# 스냅샷에서 참조 변수는 암호문 대신 이 키를 가진 객체다. Deploy Worker 가 봉인 직전에 푼다.
SNAPSHOT_REFERENCE_KEY = "reference"


def build_variables_snapshot(variables: list[ServiceVariable]) -> dict[str, Any]:
    """`{key: 암호문}`. 참조 변수는 `{key: {"reference": {serviceId, property}}}` 다."""
    snapshot: dict[str, Any] = {}
    for variable in variables:
        if variable.reference is not None:
            snapshot[variable.key] = {SNAPSHOT_REFERENCE_KEY: dict(variable.reference)}
        else:
            snapshot[variable.key] = variable.encrypted_value
    return snapshot


def database_source_sha(image_digest: str) -> str:
    """관리형 DB 요청의 source_sha. Git SHA·업로드(`upload-`)와 겹치지 않는다."""
    return f"image-{image_digest.removeprefix('sha256:')[:12]}"


class DeploymentRequestService:
    """배포 요청과 첫 job 을 함께 만든다. 트랜잭션 커밋은 호출하는 쪽이 한다.

    소스를 빌드하는 요청은 BUILD job, 이미 빌드한 이미지를 다시 쓰는 요청(롤백·재시작)은
    빌드를 건너뛰고 DEPLOY job, 서비스를 내리는 요청은 REMOVE job 으로 시작한다.

    환경변수는 요청 시점의 서비스 변수를 `variables_snapshot`(키 → 암호문)으로 저장한다.
    롤백만 원본 요청의 스냅샷을 쓴다. 재배포·재시작은 지금 변수를 써서, 변수를 고친 뒤
    다시 띄우면 고친 값이 반영된다.

    Pod 수와 리소스는 롤백을 포함해 모든 요청에서 지금 서비스의 원하는 설정을 고정한다.
    배포 방식도 같다. 서비스가 고른 방식(요청 방식)과 실제로 적용할 방식을 함께 남기고, Pod 가
    2개 미만이거나 타깃이 on-prem 이거나 기능이 꺼져 있으면 적용 방식은 ROLLING 이다. 서비스를
    내리는 요청은 Pod 를 띄우지 않으므로 둘 다 비운다.

    배포 타깃이 사용자가 등록한 서버면 그 서버가 CONNECTED 일 때만 만든다(TargetNotConnectedError).
    서비스를 내리는 요청은 막지 않는다.
    """

    def __init__(
        self,
        deployment_request_repository: DeploymentRequestRepository,
        job_repository: JobRepository,
        deployment_status_history_repository: DeploymentStatusHistoryRepository,
        build_repository: BuildRepository,
        service_variable_repository: ServiceVariableRepository,
        service_repository: ServiceRepository,
        *,
        deployment_strategy_enabled: bool = False,
        onprem_offline_after: timedelta = timedelta(
            seconds=DEFAULT_ONPREM_SERVER_OFFLINE_AFTER_SECONDS
        ),
    ) -> None:
        self._deployment_request_repository = deployment_request_repository
        self._job_repository = job_repository
        self._deployment_status_history_repository = deployment_status_history_repository
        self._build_repository = build_repository
        self._service_variable_repository = service_variable_repository
        self._service_repository = service_repository
        self._deployment_strategy_enabled = deployment_strategy_enabled
        self._onprem_offline_after = onprem_offline_after

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
        service_upload_id: int | None = None,
        is_started: bool = True,
    ) -> DeploymentRequest | None:
        """멱등성 키가 겹치거나 이 서비스에 진행 중인 배포가 있으면 만들지 않고 None 이다.

        `is_started=False` 면 BUILD job 을 만들지 않고 QUEUED 로 둔다(스택 배포가 앞 단계 성공을
        기다리는 요청). 시작은 `start_deployment_request` 로 한다.

        `source_deployment_request` 는 같은 소스로 다시 배포하는 요청(REDEPLOY)의 원본이고,
        `service_upload_id` 는 GitHub 대신 소스로 쓰는 업로드(CLI)다. 업로드를 가져가는 일은
        호출하는 쪽이 같은 트랜잭션에서 먼저 한다.
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
            service_upload_id=service_upload_id,
        )
        if request is None:
            return None
        build = await self._build_repository.add(Build(deployment_request_id=request.id))
        if is_started:
            await self._add_build_job(request.id, build.id)
        return request

    async def create_database_deployment_request(
        self,
        service: Service,
        *,
        image: ImageRef,
        trigger_type: DeploymentTrigger,
        idempotency_key: str,
        requested_by: int | None = None,
        is_started: bool = True,
    ) -> DeploymentRequest | None:
        """관리형 DB 의 배포 요청. 빌드하지 않고 고정 공식 이미지를 가리키는 성공한 빌드를 붙인다.

        소스 커밋이 없어 `source_sha` 는 `image-` + digest 앞 12자다. 시작하면 QUEUED 에서 곧바로
        DEPLOYING 이 되고 DEPLOY job 으로 간다. 진행 중인 배포가 있으면 None 이다.
        """
        request = await self._add_request(
            service,
            source_sha=database_source_sha(image.digest),
            source_commit_message=None,
            trigger_type=trigger_type,
            idempotency_key=idempotency_key,
            requested_by=requested_by,
            source_deployment_request=None,
            variables_snapshot=None,
            service_upload_id=None,
        )
        if request is None:
            return None
        await self._build_repository.add(Build.for_image(request.id, image, request.source_sha))
        if is_started:
            await self.start_deployment_request(request)
        return request

    async def start_deployment_request(self, request: DeploymentRequest) -> None:
        """QUEUED 로 기다리던 요청의 첫 job 을 만든다. 이미지가 정해진 요청은 빌드를 건너뛴다."""
        build = await self._build_repository.find_by_deployment_request_id(request.id)
        assert build is not None
        if build.status == BuildStatus.SUCCEEDED:
            await self._start_deploying(request)
            await self._job_repository.save(
                Job(
                    deployment_request_id=request.id,
                    kind=JobKind.DEPLOY,
                    payload={"build_id": build.id},
                )
            )
            return
        await self._add_build_job(request.id, build.id)

    async def _add_build_job(self, deployment_request_id: int, build_id: int) -> None:
        await self._job_repository.save(
            Job(
                deployment_request_id=deployment_request_id,
                kind=JobKind.BUILD,
                payload=BuildJobPayload(build_id=build_id).model_dump(mode="json"),
            )
        )

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
            service_upload_id=None,
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
            service_upload_id=None,
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

    async def _check_target_connected(self, service: Service) -> None:
        server = await self._service_repository.find_deploy_target_server(service.id)
        # 하트비트가 끊긴 서버(DISCONNECTED)로도 배포하지 않는다.
        if server is not None and (
            server.connection_status(datetime.now(UTC), self._onprem_offline_after)
            != OnpremServerConnectionStatus.CONNECTED
        ):
            raise TargetNotConnectedError(
                "deploy target is not connected",
                service_id=service.id,
                onprem_server_id=server.id,
                onprem_server_status=server.status,
            )

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
        variables_snapshot: dict[str, Any] | None,
        service_upload_id: int | None,
    ) -> DeploymentRequest | None:
        """`variables_snapshot` 가 None 이면 지금 서비스 변수를 스냅샷으로 저장한다."""
        # 같은 키의 재시도는 연결 상태와 상관없이 처음 만든 요청을 돌려받게 연결 확인보다 먼저 본다.
        is_replay = (
            await self._deployment_request_repository.find_by_idempotency_key(idempotency_key)
            is not None
        )
        if trigger_type != DeploymentTrigger.REMOVE and not is_replay:
            await self._check_target_connected(service)
        if variables_snapshot is None:
            variables = await self._service_variable_repository.search_by_service_id(service.id)
            variables_snapshot = build_variables_snapshot(variables)
        desired = await self._service_repository.get_deployment_settings_for_update(service.id)
        scaling = ScalingConfig.model_validate(
            desired.scaling_config or ScalingConfig.defaults().model_dump(mode="json")
        )
        requested_strategy: DeploymentStrategy | None = None
        applied_strategy: DeploymentStrategy | None = None
        if trigger_type != DeploymentTrigger.REMOVE:
            requested_strategy = desired.deployment_strategy
            applied_strategy = resolve_deployment_strategy(
                desired.deployment_strategy,
                scaling.replicas,
                target_kind=desired.target_kind,
                is_enabled=self._deployment_strategy_enabled,
            )
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
                scaling_snapshot=scaling.model_dump(mode="json"),
                requested_deployment_strategy=requested_strategy,
                deployment_strategy=applied_strategy,
                source_deployment_request_id=(
                    source_deployment_request.id if source_deployment_request is not None else None
                ),
                service_upload_id=service_upload_id,
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
