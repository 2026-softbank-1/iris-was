import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    ConflictError,
    InvalidInputError,
    ProjectNotFoundError,
    ServiceNameConflictError,
    ServiceNotFoundError,
)
from app.enums import Builder
from app.models.deployment_request import DeploymentRequest
from app.models.service import Service
from app.models.target import AWS_TARGET_NAME
from app.repositories.deployment_request_repository import DeploymentRequestRepository
from app.repositories.github_installation_repository import GithubInstallationRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.service_repository import ServiceRepository
from app.repositories.target_repository import TargetRepository
from app.services.service_teardown_service import ServiceTeardownService
from app.services.source_repository_service import SourceRepositoryService

logger = logging.getLogger(__name__)

# 서비스 이름은 이후 도메인(서브도메인)에 쓰이므로 DNS 레이블 규칙을 따른다.
SERVICE_NAME_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")

_NULLABLE_FIELDS = frozenset(
    {"root_directory", "dockerfile_path", "port", "build_command", "start_command", "builder"}
)
_NON_NULL_FIELDS = frozenset({"name", "source_branch", "is_auto_deploy", "target_ids"})


@dataclass(frozen=True)
class ServiceDetail:
    service: Service
    target_ids: list[int]
    latest_deployment: DeploymentRequest | None = None


def slugify_service_name(value: str, max_length: int = 63) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:max_length].strip("-")
    return slug or "service"


def normalize_root_directory(value: str | None) -> str | None:
    """저장소 안 상대 경로로 정리한다. 루트(빈 값·`/`·`.`)는 None 이다."""
    if value is None:
        return None
    parts = [p for p in value.strip().replace("\\", "/").split("/") if p not in ("", ".")]
    if ".." in parts:
        raise InvalidInputError(
            "root directory must stay inside the repository", field="rootDirectory"
        )
    return "/".join(parts) or None


class ServiceRegistryService:
    """서비스(사용자 앱) 등록·조회·설정. 저장소 연결은 사용자의 GitHub 접근 권한을 확인한다."""

    def __init__(
        self,
        session: AsyncSession,
        project_repository: ProjectRepository,
        service_repository: ServiceRepository,
        target_repository: TargetRepository,
        installation_repository: GithubInstallationRepository,
        source_repository_service: SourceRepositoryService,
        deployment_request_repository: DeploymentRequestRepository,
        service_teardown_service: ServiceTeardownService,
    ) -> None:
        self._session = session
        self._project_repository = project_repository
        self._service_repository = service_repository
        self._service_teardown_service = service_teardown_service
        self._target_repository = target_repository
        self._installation_repository = installation_repository
        self._source_repository_service = source_repository_service
        self._deployment_request_repository = deployment_request_repository

    async def create_service(
        self,
        owner_id: int,
        project_id: int,
        repository_url: str,
        name: str | None,
        branch: str | None,
        root_directory: str | None,
        is_auto_deploy: bool,
        target_ids: list[int] | None,
    ) -> ServiceDetail:
        project = await self._project_repository.find_by_id_and_owner_id(project_id, owner_id)
        if project is None:
            raise ProjectNotFoundError("project not found", project_id=project_id)

        repository = await self._source_repository_service.resolve_repository(
            owner_id, repository_url
        )
        if branch is None:
            branch = repository.default_branch
        else:
            await self._ensure_branch_exists(owner_id, repository.full_name, branch)
        service_name = name or slugify_service_name(repository.full_name.split("/", 1)[1])
        await self._ensure_name_available(project.id, service_name)
        resolved_target_ids = await self._resolve_target_ids(target_ids)
        installation = await self._installation_repository.find_by_installation_id(
            repository.installation_id
        )
        if installation is None:
            raise ServiceNotFoundError("github installation not found")

        service = await self._service_repository.save(
            Service(
                project_id=project.id,
                name=service_name,
                source_repository_url=repository.url,
                github_installation_id=installation.id,
                source_branch=branch,
                root_directory=normalize_root_directory(root_directory),
                is_auto_deploy=is_auto_deploy,
            )
        )
        await self._service_repository.replace_targets(service.id, set(resolved_target_ids))
        await self._session.commit()
        logger.info(
            "service created",
            extra={"action": "create_service", "project_id": project.id, "service_id": service.id},
        )
        return ServiceDetail(service, resolved_target_ids)

    async def get_service(self, owner_id: int, service_id: int) -> ServiceDetail:
        service = await self._get_owned(owner_id, service_id)
        return (await self._detail([service]))[0]

    async def search_services(self, owner_id: int, project_id: int) -> list[ServiceDetail]:
        project = await self._project_repository.find_by_id_and_owner_id(project_id, owner_id)
        if project is None:
            raise ProjectNotFoundError("project not found", project_id=project_id)
        services = await self._service_repository.search_by_project_id(project.id)
        return await self._detail(services)

    async def update_service(
        self, owner_id: int, service_id: int, changes: Mapping[str, Any]
    ) -> ServiceDetail:
        """`changes` 에 있는 키만 바꾼다. 명시한 null 은 값을 비운다(비울 수 없는 필드는 거부)."""
        service = await self._get_owned(owner_id, service_id)
        for field in changes:
            if field not in _NULLABLE_FIELDS | _NON_NULL_FIELDS:
                raise InvalidInputError("field cannot be updated", field=field)
            if field in _NON_NULL_FIELDS and changes[field] is None:
                raise InvalidInputError("field cannot be null", field=field)

        if "name" in changes and changes["name"] != service.name:
            await self._ensure_name_available(service.project_id, changes["name"])
        if "source_branch" in changes and changes["source_branch"] != service.source_branch:
            full_name = _full_name(service.source_repository_url)
            await self._ensure_branch_exists(owner_id, full_name, changes["source_branch"])

        for field, value in changes.items():
            if field == "target_ids":
                continue
            if field == "root_directory":
                value = normalize_root_directory(value)
            if field == "builder" and value is not None:
                value = Builder(value)
            setattr(service, field, value)

        if "target_ids" in changes:
            target_ids = await self._resolve_target_ids(changes["target_ids"])
            await self._ensure_target_kept_after_deploy(service.id, target_ids)
            await self._service_repository.replace_targets(service.id, set(target_ids))
        await self._session.commit()
        return (await self._detail([service]))[0]

    async def delete_service(self, owner_id: int, service_id: int) -> None:
        """소프트 삭제하고 떠 있는 앱도 내린다(REMOVE 요청). 진행 중인 배포가 있으면 안 지운다."""
        service = await self._get_owned(owner_id, service_id)
        await self._service_teardown_service.request_teardown([service], owner_id)
        service.mark_as_deleted()
        await self._session.commit()
        logger.info("service deleted", extra={"action": "delete_service", "service_id": service_id})

    async def _get_owned(self, owner_id: int, service_id: int) -> Service:
        service = await self._service_repository.find_by_id_and_owner_id(service_id, owner_id)
        if service is None:
            raise ServiceNotFoundError("service not found", service_id=service_id)
        return service

    async def _ensure_name_available(self, project_id: int, name: str) -> None:
        if not SERVICE_NAME_PATTERN.match(name):
            raise InvalidInputError(
                "service name must be lowercase letters, digits and hyphens", field="name"
            )
        if await self._service_repository.find_by_project_id_and_name(project_id, name):
            raise ServiceNameConflictError("service name already exists", service_name=name)

    async def _ensure_branch_exists(self, owner_id: int, full_name: str, branch: str) -> None:
        branches = await self._source_repository_service.search_branches(owner_id, full_name)
        if branch not in {b.name for b in branches}:
            raise InvalidInputError("branch not found in repository", field="branch", branch=branch)

    async def _resolve_target_ids(self, target_ids: list[int] | None) -> list[int]:
        """서비스는 타깃 하나에만 배포한다. 지정이 없으면 `aws` 타깃이다."""
        if target_ids is None:
            targets = await self._target_repository.search_all()
            return [t.id for t in targets if t.name == AWS_TARGET_NAME]
        wanted = sorted(set(target_ids))
        if len(wanted) != 1:
            raise InvalidInputError("exactly one target is required", field="targetIds")
        if not await self._target_repository.search_by_ids(wanted):
            raise InvalidInputError("unknown target", field="targetIds", target_ids=wanted)
        return wanted

    async def _ensure_target_kept_after_deploy(
        self, service_id: int, target_ids: list[int]
    ) -> None:
        """배포한 뒤 타깃을 바꾸면 두 ApplicationSet 이 같은 svc-{id} 를 만든다."""
        current = (await self._service_repository.search_target_ids_by_service_ids([service_id]))[
            service_id
        ]
        if current == target_ids:
            return
        if await self._deployment_request_repository.count_by_service_id(service_id):
            raise ConflictError("target cannot change after deployment", field="targetIds")

    async def _detail(self, services: list[Service]) -> list[ServiceDetail]:
        target_ids = await self._service_repository.search_target_ids_by_service_ids(
            [s.id for s in services]
        )
        latest = await self._deployment_request_repository.search_latest_by_service_ids(
            [s.id for s in services]
        )
        return [ServiceDetail(s, target_ids.get(s.id, []), latest.get(s.id)) for s in services]


def _full_name(repository_url: str) -> str:
    return repository_url.removeprefix("https://github.com/")
