import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    InvalidInputError,
    ProjectNameConflictError,
    ProjectNotFoundError,
)
from app.models.project import Project
from app.repositories.project_repository import ProjectRepository, ServiceCounts
from app.repositories.service_repository import ServiceRepository

logger = logging.getLogger(__name__)

_NO_SERVICES = ServiceCounts(service_count=0, online_service_count=0)


@dataclass(frozen=True)
class ProjectSummary:
    project: Project
    counts: ServiceCounts


class ProjectService:
    """프로젝트(서비스를 묶는 단위). 소유자만 접근한다. 남의 프로젝트는 없는 것으로 답한다."""

    def __init__(
        self,
        session: AsyncSession,
        project_repository: ProjectRepository,
        service_repository: ServiceRepository,
    ) -> None:
        self._session = session
        self._project_repository = project_repository
        self._service_repository = service_repository

    async def create_project(
        self, owner_id: int, name: str, description: str | None
    ) -> ProjectSummary:
        await self._ensure_name_available(owner_id, name)
        project = await self._project_repository.save(
            Project(name=name, description=description, owner_id=owner_id)
        )
        await self._session.commit()
        logger.info("project created", extra={"action": "create_project", "project_id": project.id})
        return ProjectSummary(project, _NO_SERVICES)

    async def get_project(self, owner_id: int, project_id: int) -> ProjectSummary:
        project = await self._get_owned(owner_id, project_id)
        return (await self._summarize([project]))[0]

    async def search_projects(self, owner_id: int) -> list[ProjectSummary]:
        projects = await self._project_repository.search_by_owner_id(owner_id)
        return await self._summarize(projects)

    async def update_project(
        self, owner_id: int, project_id: int, changes: Mapping[str, Any]
    ) -> ProjectSummary:
        """`changes` 에 있는 키만 바꾼다. description 은 null 로 비울 수 있다."""
        project = await self._get_owned(owner_id, project_id)
        if "name" in changes:
            name = changes["name"]
            if name is None:
                raise InvalidInputError("name cannot be null", field="name")
            if name != project.name:
                await self._ensure_name_available(owner_id, name)
            project.name = name
        if "description" in changes:
            project.description = changes["description"]
        await self._session.commit()
        return (await self._summarize([project]))[0]

    async def delete_project(self, owner_id: int, project_id: int) -> None:
        """프로젝트와 소속 서비스를 소프트 삭제한다. 실행 중인 리소스 정리는 후속 단계의 일이다."""
        project = await self._get_owned(owner_id, project_id)
        project.mark_as_deleted()
        await self._service_repository.mark_as_deleted_by_project_id(project.id)
        await self._session.commit()
        logger.info("project deleted", extra={"action": "delete_project", "project_id": project_id})

    async def _get_owned(self, owner_id: int, project_id: int) -> Project:
        project = await self._project_repository.find_by_id_and_owner_id(project_id, owner_id)
        if project is None:
            raise ProjectNotFoundError("project not found", project_id=project_id)
        return project

    async def _ensure_name_available(self, owner_id: int, name: str) -> None:
        if await self._project_repository.find_by_owner_id_and_name(owner_id, name) is not None:
            raise ProjectNameConflictError("project name already exists", project_name=name)

    async def _summarize(self, projects: list[Project]) -> list[ProjectSummary]:
        counts = await self._project_repository.count_services_by_project_ids(
            [p.id for p in projects]
        )
        return [ProjectSummary(p, counts.get(p.id, _NO_SERVICES)) for p in projects]
