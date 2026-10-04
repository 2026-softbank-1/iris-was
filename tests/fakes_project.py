"""프로젝트·서비스 계층 테스트용 인메모리 Repository."""

from itertools import count

from app.enums import DeploymentStrategy, TargetKind
from app.models.base import now_utc
from app.models.onprem_server import OnpremServer
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
        self.targets: dict[int, set[int]] = {}
        # 서비스 id → 그 서비스의 배포 타깃인 등록 서버.
        self.servers: dict[int, OnpremServer] = {}
        self._ids = count(1)

    async def find_deploy_target_server(self, service_id: int) -> OnpremServer | None:
        return self.servers.get(service_id)

    async def search_ids_by_target_id(self, target_id: int) -> list[int]:
        return sorted(
            service_id
            for service_id, target_ids in self.targets.items()
            if target_id in target_ids and not self.services[service_id].is_deleted
        )

    async def is_target_in_use(self, target_id: int) -> bool:
        return bool(await self.search_ids_by_target_id(target_id))

    async def get_deployment_settings_for_update(self, service_id: int) -> DeploymentSettings:
        service = self.services[service_id]
        # FakeTargetRepository 와 같은 id 다: 1 = aws(AWS), 2 = onprem(ONPREM).
        # 그 밖의 id 는 테스트가 붙인 등록 서버 타깃(ONPREM)이다.
        kinds = {
            TARGET_KINDS.get(t, TargetKind.ONPREM) for t in self.targets.get(service_id, set())
        }
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
        aws = Target(name="aws", kind=TargetKind.AWS, is_deleted=False)
        aws.id = 1
        onprem = Target(name="onprem", kind=TargetKind.ONPREM, is_deleted=False)
        onprem.id = 2
        self.targets = [aws, onprem]

    async def search_all(self) -> list[Target]:
        return list(self.targets)

    async def search_by_ids(self, target_ids: list[int]) -> list[Target]:
        return [t for t in self.targets if t.id in target_ids]

    async def search_visible(self, owner_id: int) -> list[Target]:
        return [t for t in self.targets if not t.is_deleted and t.owner_id in (None, owner_id)]

    async def search_visible_by_ids(self, target_ids: list[int], owner_id: int) -> list[Target]:
        return [t for t in await self.search_visible(owner_id) if t.id in target_ids]

    async def add(self, target: Target) -> Target:
        target.id = max(t.id for t in self.targets) + 1
        target.is_deleted = False
        self.targets.append(target)
        return target


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
