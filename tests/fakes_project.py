"""프로젝트·서비스 계층 테스트용 인메모리 Repository."""

from itertools import count
from typing import Any

from app.enums import TargetKind
from app.models.base import now_utc
from app.models.project import Project
from app.models.service import Service
from app.models.target import Target
from app.repositories.project_repository import ServiceCounts


class FakeProjectRepository:
    def __init__(self) -> None:
        self.projects: dict[int, Project] = {}
        self.counts: dict[int, ServiceCounts] = {}
        self._ids = count(1)

    async def find_by_id_and_owner_id(self, project_id: int, owner_id: int) -> Project | None:
        project = self.projects.get(project_id)
        if project is None or project.owner_id != owner_id or project.is_deleted:
            return None
        return project

    async def find_by_owner_id_and_name(self, owner_id: int, name: str) -> Project | None:
        return next(
            (
                p
                for p in self.projects.values()
                if p.owner_id == owner_id and p.name == name and not p.is_deleted
            ),
            None,
        )

    async def search_by_owner_id(self, owner_id: int) -> list[Project]:
        found = [p for p in self.projects.values() if p.owner_id == owner_id and not p.is_deleted]
        return sorted(found, key=lambda p: p.id, reverse=True)

    async def count_services_by_project_ids(
        self, project_ids: list[int]
    ) -> dict[int, ServiceCounts]:
        return {i: self.counts[i] for i in project_ids if i in self.counts}

    async def save(self, project: Project) -> Project:
        if project.id is None:
            project.id = next(self._ids)
            project.created_at = project.updated_at = now_utc()
            project.is_deleted = False
        self.projects[project.id] = project
        return project


class FakeServiceRepository:
    def __init__(self, projects: FakeProjectRepository) -> None:
        self._projects = projects
        self.services: dict[int, Service] = {}
        self.targets: dict[int, set[int]] = {}
        self._ids = count(1)

    async def get_scaling_config_for_update(self, service_id: int) -> dict[str, Any] | None:
        return self.services[service_id].scaling_config

    async def find_by_id_and_owner_id(
        self, service_id: int, owner_id: int, *, for_update: bool = False
    ) -> Service | None:
        service = self.services.get(service_id)
        if service is None or service.is_deleted:
            return None
        project = await self._projects.find_by_id_and_owner_id(service.project_id, owner_id)
        return service if project is not None else None

    async def find_by_project_id_and_name(self, project_id: int, name: str) -> Service | None:
        return next(
            (
                s
                for s in self.services.values()
                if s.project_id == project_id and s.name == name and not s.is_deleted
            ),
            None,
        )

    async def search_by_project_id(self, project_id: int) -> list[Service]:
        return [
            s for s in self.services.values() if s.project_id == project_id and not s.is_deleted
        ]

    async def search_target_ids_by_service_ids(
        self, service_ids: list[int]
    ) -> dict[int, list[int]]:
        return {i: sorted(self.targets.get(i, set())) for i in service_ids}

    async def save(self, service: Service) -> Service:
        if service.id is None:
            service.id = next(self._ids)
            service.created_at = service.updated_at = now_utc()
            service.is_deleted = False
            service.platform = service.platform or "linux/amd64"
            if service.is_auto_deploy is None:
                service.is_auto_deploy = True
        self.services[service.id] = service
        return service

    async def replace_targets(self, service_id: int, target_ids: set[int]) -> None:
        self.targets[service_id] = set(target_ids)

    async def mark_as_deleted_by_project_id(self, project_id: int) -> None:
        for service in self.services.values():
            if service.project_id == project_id:
                service.mark_as_deleted()


class FakeTargetRepository:
    def __init__(self) -> None:
        aws = Target(name="aws", kind=TargetKind.AWS)
        aws.id = 1
        local = Target(name="local", kind=TargetKind.LOCAL)
        local.id = 2
        self.targets = [aws, local]

    async def search_all(self) -> list[Target]:
        return list(self.targets)

    async def search_by_ids(self, target_ids: list[int]) -> list[Target]:
        return [t for t in self.targets if t.id in target_ids]
