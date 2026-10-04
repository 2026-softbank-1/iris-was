import logging
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    DeploymentInProgressError,
    FieldIssue,
    InvalidInputError,
    NoSucceededDeploymentError,
    ServiceNotFoundError,
)
from app.enums import ACTIVE_DEPLOYMENT_STATUSES, BuildStatus, DeploymentTrigger, ServiceKind
from app.repositories.build_repository import BuildRepository
from app.repositories.deployment_request_repository import DeploymentRequestRepository
from app.repositories.service_repository import ServiceRepository
from app.services.deployment_request_service import DeploymentRequestService
from app.services.scaling_config import ScalingConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ServiceScalingDetail:
    service_id: int
    scaling: ScalingConfig
    deployment_request_id: int | None = None


class ServiceScalingService:
    """원하는 사양을 저장하고 현재 이미지를 재사용하는 배포 요청을 함께 만든다."""

    def __init__(
        self,
        session: AsyncSession,
        service_repository: ServiceRepository,
        deployment_request_repository: DeploymentRequestRepository,
        build_repository: BuildRepository,
        deployment_request_service: DeploymentRequestService,
    ) -> None:
        self._session = session
        self._service_repository = service_repository
        self._deployment_request_repository = deployment_request_repository
        self._build_repository = build_repository
        self._deployment_request_service = deployment_request_service

    async def get_scaling(self, owner_id: int, service_id: int) -> ServiceScalingDetail:
        service = await self._service_repository.find_by_id_and_owner_id(service_id, owner_id)
        if service is None:
            raise ServiceNotFoundError("service not found", service_id=service_id)
        return ServiceScalingDetail(service.id, _config(service.scaling_config))

    async def update_scaling(
        self,
        owner_id: int,
        service_id: int,
        config: ScalingConfig,
        *,
        idempotency_key: str | None = None,
    ) -> ServiceScalingDetail:
        service = await self._service_repository.find_by_id_and_owner_id(
            service_id, owner_id, for_update=True
        )
        if service is None:
            raise ServiceNotFoundError("service not found", service_id=service_id)
        if service.kind == ServiceKind.DATABASE and config.replicas > 1:
            # 관리형 DB 는 단일 인스턴스다. 0 은 정지, 1 은 실행이다.
            raise InvalidInputError(
                "database services run a single instance",
                issues=[FieldIssue("replicas", "database_single_instance")],
                field="replicas",
                service_id=service.id,
            )
        key = f"scaling:{service.id}:{idempotency_key or uuid4()}"
        if idempotency_key is not None:
            replayed = await self._deployment_request_repository.find_by_idempotency_key(key)
            if replayed is not None:
                if _config(replayed.scaling_snapshot) != config:
                    raise InvalidInputError(
                        "idempotency key was already used with different scaling settings",
                        field="Idempotency-Key",
                    )
                return ServiceScalingDetail(service.id, config, replayed.id)

        latest = (
            await self._deployment_request_repository.search_latest_by_service_ids([service.id])
        ).get(service.id)
        if latest is not None and latest.status in ACTIVE_DEPLOYMENT_STATUSES:
            raise DeploymentInProgressError(
                "a deployment is already in progress", service_id=service.id
            )
        live = await self._deployment_request_repository.find_latest_succeeded_by_service_id(
            service.id
        )
        if live is None or live.trigger_type == DeploymentTrigger.REMOVE:
            raise NoSucceededDeploymentError("no running deployment", service_id=service.id)
        build = await self._build_repository.find_by_deployment_request_id(live.id)
        if (
            build is None
            or build.status != BuildStatus.SUCCEEDED
            or build.image_digest is None
            or build.image_repository is None
        ):
            raise InvalidInputError("running deployment has no built image", service_id=service.id)
        # 실패 후 원하는 사양과 적용된 사양이 다르면 같은 PUT 으로 다시 적용할 수 있다.
        if (
            idempotency_key is None
            and _config(service.scaling_config) == config
            and _config(live.scaling_snapshot) == config
        ):
            return ServiceScalingDetail(service.id, config, live.id)

        previous_config = service.scaling_config
        service.scaling_config = config.model_dump(mode="json")
        try:
            request = (
                await self._deployment_request_service.create_deployment_request_reusing_image(
                    service,
                    source_deployment_request=live,
                    source_build=build,
                    trigger_type=DeploymentTrigger.RESTART,
                    idempotency_key=key,
                    requested_by=owner_id,
                )
            )
            if request is None:
                raise DeploymentInProgressError(
                    "a deployment is already in progress", service_id=service.id
                )
            await self._session.commit()
        except Exception:
            service.scaling_config = previous_config
            raise
        logger.info(
            "service scaling requested",
            extra={
                "action": "update_scaling",
                "service_id": service.id,
                "deployment_request_id": request.id,
                "replicas": config.replicas,
            },
        )
        return ServiceScalingDetail(service.id, config, request.id)


def _config(value: object) -> ScalingConfig:
    return ScalingConfig.defaults() if value is None else ScalingConfig.model_validate(value)
