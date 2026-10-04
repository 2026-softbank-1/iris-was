"""관리형 DB 서비스(개발·데모용 단일 인스턴스) 생성. 빌드 없이 고정 공식 이미지로 배포한다.

자격 증명은 만들 때 한 번 생성해 DB 서비스의 암호화 변수로 둔다(엔진별 이름). 비밀번호는 응답·로그에
남기지 않고, 앱은 참조 변수(`{serviceId, property: url}`)로 받는다. 서비스를 지우면 GitOps
디렉터리가
지워지고 chart 가 PVC 까지 지운다(데이터가 사라진다. 백업 없음).
"""

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import VariableCipher
from app.core.exceptions import FieldIssue, InvalidInputError, NotConfiguredError
from app.enums import DatabaseEngine, DeploymentTrigger, ServiceKind, TargetKind
from app.models.deployment_request import DeploymentRequest
from app.models.project import Project
from app.models.service import Service
from app.repositories.service_repository import ServiceRepository
from app.repositories.service_variable_repository import ServiceVariableRepository
from app.services.database_engines import (
    DEFAULT_STORAGE_GI,
    MAX_STORAGE_GI,
    MIN_STORAGE_GI,
    generate_credentials,
    get_engine_spec,
    resolve_image,
)
from app.services.deployment_request_service import DeploymentRequestService
from app.services.service_networking import is_networking_available, networking_unavailable_reason
from app.services.service_registry_service import ServiceDetail, ServiceRegistryService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DatabasePlan:
    name: str
    engine: DatabaseEngine
    storage_gi: int = DEFAULT_STORAGE_GI
    user: str | None = None
    database: str | None = None
    stack_id: int | None = None
    stack_unit_id: str | None = None


def check_networking(is_enabled: bool, target_kind: TargetKind, field: str) -> None:
    """관리형 DB·별칭·참조 변수는 chart 0.8.0 이 배포된 AWS 타깃에서만 쓴다."""
    if not is_networking_available(is_enabled, target_kind):
        raise InvalidInputError(
            "project databases and host aliases are not available for this target",
            issues=[FieldIssue(field, networking_unavailable_reason(is_enabled))],
            field=field,
            target_kind=target_kind,
        )


class DatabaseService:
    def __init__(
        self,
        session: AsyncSession,
        service_registry_service: ServiceRegistryService,
        service_repository: ServiceRepository,
        service_variable_repository: ServiceVariableRepository,
        deployment_request_service: DeploymentRequestService,
        cipher: VariableCipher | None,
        *,
        is_networking_enabled: bool,
        database_images: dict[str, str] | None = None,
    ) -> None:
        self._session = session
        self._service_registry_service = service_registry_service
        self._service_repository = service_repository
        self._service_variable_repository = service_variable_repository
        self._deployment_request_service = deployment_request_service
        self._cipher = cipher
        self._is_networking_enabled = is_networking_enabled
        self._database_images = database_images or {}

    async def create_database(
        self,
        owner_id: int,
        project_id: int,
        name: str,
        engine: DatabaseEngine,
        storage_gi: int | None,
        target_ids: list[int] | None,
    ) -> ServiceDetail:
        """DB 서비스를 만들고 첫 배포를 접수한다(201). 같은 이름이 있으면 409."""
        project = await self._service_registry_service.get_project(owner_id, project_id)
        resolved_target_ids, target_kind = await self._service_registry_service.resolve_targets(
            target_ids
        )
        check_networking(self._is_networking_enabled, target_kind, "engine")
        service = await self.add_database(
            project,
            DatabasePlan(name=name, engine=engine, storage_gi=storage_gi or DEFAULT_STORAGE_GI),
            resolved_target_ids,
        )
        request = await self.request_first_deployment(service, owner_id)
        await self._session.commit()
        logger.info(
            "database created",
            extra={
                "action": "create_database",
                "project_id": project.id,
                "service_id": service.id,
                "database_engine": engine,
            },
        )
        return ServiceDetail(service, resolved_target_ids, request)

    async def add_database(
        self, project: Project, plan: DatabasePlan, target_ids: list[int]
    ) -> Service:
        """DB 서비스와 자격 증명 변수를 만든다. 커밋하지 않는다(apply 가 같은 트랜잭션에 묶는다)."""
        if self._cipher is None:
            raise NotConfiguredError(
                "variables encryption is not configured", setting="VARIABLES_ENCRYPTION_KEY"
            )
        if not MIN_STORAGE_GI <= plan.storage_gi <= MAX_STORAGE_GI:
            raise InvalidInputError(
                "database storage must be between 1 and 20 GiB",
                issues=[FieldIssue("storageGi", "out_of_range")],
                field="storageGi",
            )
        await self._service_registry_service.ensure_name_available(project.id, plan.name)
        spec = get_engine_spec(plan.engine)
        image = resolve_image(plan.engine, self._database_images)
        credentials = generate_credentials(plan.engine, plan.user, plan.database)
        service = await self._service_repository.save(
            Service(
                project_id=project.id,
                name=plan.name,
                # 소스가 없다. 푸시·빌드 대상이 아니다.
                source_repository_url="",
                github_installation_id=None,
                source_branch="",
                is_auto_deploy=False,
                kind=ServiceKind.DATABASE,
                database_engine=plan.engine,
                database_config={
                    "image": image.reference,
                    "storageGi": plan.storage_gi,
                    "port": spec.port,
                    "user": credentials.get(spec.user_key) if spec.user_key else "default",
                    "database": credentials.get(spec.database_key) if spec.database_key else None,
                },
                stack_id=plan.stack_id,
                stack_unit_id=plan.stack_unit_id,
            )
        )
        await self._service_repository.replace_targets(service.id, set(target_ids))
        for key, value in credentials.items():
            await self._service_variable_repository.add_if_absent(
                service.id, key, self._cipher.encrypt(value)
            )
        return service

    async def request_first_deployment(
        self, service: Service, owner_id: int | None
    ) -> DeploymentRequest | None:
        assert service.database_engine is not None
        return await self._deployment_request_service.create_database_deployment_request(
            service,
            image=resolve_image(DatabaseEngine(service.database_engine), self._database_images),
            trigger_type=DeploymentTrigger.MANUAL,
            idempotency_key=f"database:{service.id}:create",
            requested_by=owner_id,
        )
