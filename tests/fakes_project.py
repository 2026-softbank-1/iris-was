"""프로젝트·서비스 계층 테스트용 인메모리 Repository."""

from itertools import count

from app.enums import DeploymentStrategy, ServiceKind, TargetKind
from app.models.base import now_utc
from app.models.project import Project
from app.models.service import Service
from app.models.target import Target
from app.repositories.project_repository import ServiceCounts
from app.repositories.service_repository import DeploymentSettings


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


TARGET_KINDS = {1: TargetKind.AWS, 2: TargetKind.ONPREM}


class FakeServiceRepository:
    def __init__(self, projects: FakeProjectRepository) -> None:
        self._projects = projects
        self.services: dict[int, Service] = {}
        self.target_kinds: dict[int, TargetKind] = {}
        self.targets: dict[int, set[int]] = {}
        self._ids = count(1)

    async def get_deployment_settings_for_update(self, service_id: int) -> DeploymentSettings:
        service = self.services[service_id]
        # FakeTargetRepository 와 같은 id 다: 1 = aws(AWS), 2 = onprem(ONPREM).
        kinds = {TARGET_KINDS[t] for t in self.targets.get(service_id, set())}
        return DeploymentSettings(
            service.scaling_config,
            service.deployment_strategy or DeploymentStrategy.ROLLING,
            TargetKind.ONPREM if TargetKind.ONPREM in kinds else TargetKind.AWS,
        )

    async def find_by_id_and_owner_id(
        self, service_id: int, owner_id: int, *, for_update: bool = False
    ) -> Service | None:
        service = self.services.get(service_id)
        if service is None or service.is_deleted:
            return None
        project = await self._projects.find_by_id_and_owner_id(service.project_id, owner_id)
        return service if project is not None else None

    async def find_owner_id_by_id(self, service_id: int) -> int | None:
        service = self.services.get(service_id)
        if service is None or service.is_deleted:
            return None
        project = self._projects.projects.get(service.project_id)
        return None if project is None or project.is_deleted else project.owner_id

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
            service.deployment_strategy = service.deployment_strategy or DeploymentStrategy.ROLLING
            service.kind = service.kind or ServiceKind.APP
            if service.is_auto_deploy is None:
                service.is_auto_deploy = True
        self.services[service.id] = service
        return service

    async def replace_targets(self, service_id: int, target_ids: set[int]) -> None:
        self.targets[service_id] = set(target_ids)

    async def find_active_by_id(self, service_id: int) -> Service | None:
        service = self.services.get(service_id)
        return service if service is not None and not service.is_deleted else None

    async def find_target_kind(self, service_id: int) -> TargetKind:
        return self.target_kinds.get(service_id, TargetKind.AWS)

    async def search_by_stack_id(self, stack_id: int) -> list[Service]:
        return [s for s in self.services.values() if s.stack_id == stack_id and not s.is_deleted]

    async def flush(self) -> None:
        return None

    async def mark_as_deleted_by_project_id(self, project_id: int) -> None:
        for service in self.services.values():
            if service.project_id == project_id:
                service.mark_as_deleted()


class FakeTargetRepository:
    def __init__(self) -> None:
        aws = Target(name="aws", kind=TargetKind.AWS)
        aws.id = 1
        onprem = Target(name="onprem", kind=TargetKind.ONPREM)
        onprem.id = 2
        self.targets = [aws, onprem]

    async def search_all(self) -> list[Target]:
        return list(self.targets)

    async def search_by_ids(self, target_ids: list[int]) -> list[Target]:
        return [t for t in self.targets if t.id in target_ids]


class FakeTeardownService:
    """서비스 삭제 테스트용 대역. 호출을 기록하고, error 가 있으면 던진다."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[list[int], int]] = []
        self.error = error

    async def request_teardown(self, services: list[Service], requested_by: int) -> int:
        if self.error is not None:
            raise self.error
        self.calls.append(([s.id for s in services], requested_by))
        return len(services)
