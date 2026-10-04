from typing import Any, NamedTuple

from sqlalchemy import delete, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import ACTIVE_DEPLOYMENT_STATUSES, DeploymentStrategy, ServiceKind, TargetKind
from app.models.base import now_utc
from app.models.deployment_request import DeploymentRequest
from app.models.onprem_server import OnpremServer
from app.models.project import Project
from app.models.release import Release
from app.models.service import Service
from app.models.target import ServiceTarget, Target
from app.repositories.release_repository import removed_after_release


class DeploymentSettings(NamedTuple):
    """배포 요청이 고정하는 서비스 설정. 요청 시점의 최신 값이다."""

    scaling_config: dict[str, Any] | None
    deployment_strategy: DeploymentStrategy
    target_kind: TargetKind


class ServiceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_id_and_owner_id(
        self, service_id: int, owner_id: int, *, for_update: bool = False
    ) -> Service | None:
        """프로젝트 소유자 기준으로 서비스를 찾는다. 삭제된 서비스·프로젝트는 없는 것으로 본다."""
        stmt = (
            select(Service)
            .join(Project, Project.id == Service.project_id)
            .where(
                Service.id == service_id,
                Project.owner_id == owner_id,
                Service.is_deleted.is_(False),
                Project.is_deleted.is_(False),
            )
        )
        if for_update:
            stmt = stmt.with_for_update(of=Service).execution_options(populate_existing=True)
        return (await self._session.scalars(stmt)).one_or_none()

    async def find_owner_id_by_id(self, service_id: int) -> int | None:
        """서비스를 가진 프로젝트의 소유자. 삭제된 서비스·프로젝트는 없는 것으로 본다."""
        stmt = (
            select(Project.owner_id)
            .join(Service, Service.project_id == Project.id)
            .where(
                Service.id == service_id,
                Service.is_deleted.is_(False),
                Project.is_deleted.is_(False),
            )
        )
        return (await self._session.scalars(stmt)).one_or_none()

    async def get_deployment_settings_for_update(self, service_id: int) -> DeploymentSettings:
        """원하는 Pod 설정·배포 방식·배포 타깃 종류를 읽고 배포 요청이 커밋될 때까지 행을 잠근다.

        타깃이 없는 서비스는 `aws` 타깃에 배포하므로 AWS 다.
        """
        # PUT이 같은 트랜잭션에서 바꾼 값도 DB에서 읽을 수 있도록 먼저 반영한다.
        await self._session.flush()
        target_kind = (
            select(Target.kind)
            .join(ServiceTarget, ServiceTarget.target_id == Target.id)
            .where(ServiceTarget.service_id == Service.id)
            .order_by(Target.id)
            .limit(1)
            .scalar_subquery()
        )
        stmt = (
            select(Service.scaling_config, Service.deployment_strategy, target_kind)
            .where(Service.id == service_id)
            .with_for_update(of=Service)
        )
        scaling_config, deployment_strategy, kind = (await self._session.execute(stmt)).one()
        return DeploymentSettings(scaling_config, deployment_strategy, kind or TargetKind.AWS)

    async def find_active_by_id(self, service_id: int) -> Service | None:
        """지워지지 않은 서비스(프로젝트도 지워지지 않은 것)."""
        stmt = (
            select(Service)
            .join(Project, Project.id == Service.project_id)
            .where(
                Service.id == service_id,
                Service.is_deleted.is_(False),
                Project.is_deleted.is_(False),
            )
        )
        return (await self._session.scalars(stmt)).one_or_none()

    async def find_target_kind(self, service_id: int) -> TargetKind:
        """서비스가 배포되는 타깃의 종류. 타깃이 없으면 `aws` 에 배포하므로 AWS 다."""
        stmt = (
            select(Target.kind)
            .join(ServiceTarget, ServiceTarget.target_id == Target.id)
            .where(ServiceTarget.service_id == service_id)
            .order_by(Target.id)
            .limit(1)
        )
        return (await self._session.scalar(stmt)) or TargetKind.AWS

    async def search_by_stack_id(self, stack_id: int) -> list[Service]:
        stmt = (
            select(Service)
            .where(Service.stack_id == stack_id, Service.is_deleted.is_(False))
            .order_by(Service.id)
        )
        return list((await self._session.scalars(stmt)).all())

    async def search_by_ids(self, service_ids: list[int]) -> list[Service]:
        """지워지지 않은 서비스만 id 순서로."""
        if not service_ids:
            return []
        stmt = (
            select(Service)
            .where(Service.id.in_(service_ids), Service.is_deleted.is_(False))
            .order_by(Service.id)
        )
        return list((await self._session.scalars(stmt)).all())

    async def find_by_project_id_and_name(self, project_id: int, name: str) -> Service | None:
        stmt = select(Service).where(
            Service.project_id == project_id, Service.name == name, Service.is_deleted.is_(False)
        )
        return (await self._session.scalars(stmt)).one_or_none()

    async def search_auto_deploy_by_repository_url_and_branch(
        self, repository_url: str, branch: str
    ) -> list[Service]:
        """푸시가 온 저장소·브랜치에 연결된 자동 배포 서비스. 주소는 대소문자를 가리지 않는다."""
        stmt = (
            select(Service)
            .join(Project, Project.id == Service.project_id)
            .where(
                func.lower(Service.source_repository_url) == repository_url.lower(),
                Service.source_branch == branch,
                Service.is_auto_deploy.is_(True),
                # 관리형 DB 는 소스가 없어 푸시로 다시 배포하지 않는다.
                Service.kind == ServiceKind.APP,
                Service.is_deleted.is_(False),
                Project.is_deleted.is_(False),
            )
            .order_by(Service.id)
        )
        return list((await self._session.scalars(stmt)).all())

    async def search_by_project_id(self, project_id: int) -> list[Service]:
        stmt = (
            select(Service)
            .where(Service.project_id == project_id, Service.is_deleted.is_(False))
            .order_by(Service.created_at, Service.id)
        )
        return list((await self._session.scalars(stmt)).all())

    async def search_target_ids_by_service_ids(
        self, service_ids: list[int]
    ) -> dict[int, list[int]]:
        stmt = (
            select(ServiceTarget.service_id, ServiceTarget.target_id)
            .where(ServiceTarget.service_id.in_(service_ids))
            .order_by(ServiceTarget.service_id, ServiceTarget.target_id)
        )
        target_ids: dict[int, list[int]] = {service_id: [] for service_id in service_ids}
        for service_id, target_id in (await self._session.execute(stmt)).all():
            target_ids[service_id].append(target_id)
        return target_ids

    async def flush(self) -> None:
        await self._session.flush()

    async def find_deploy_target_server(self, service_id: int) -> OnpremServer | None:
        """서비스의 배포 타깃이 사용자가 등록한 서버면 그 서버. 공용 타깃이면 None 이다."""
        stmt = (
            select(OnpremServer)
            .join(ServiceTarget, ServiceTarget.target_id == OnpremServer.target_id)
            .where(ServiceTarget.service_id == service_id)
            .limit(1)
        )
        return (await self._session.scalars(stmt)).one_or_none()

    async def search_ids_by_target_id(self, target_id: int) -> list[int]:
        """타깃에 붙은(삭제되지 않은) 서비스 id."""
        stmt = (
            select(Service.id)
            .join(ServiceTarget, ServiceTarget.service_id == Service.id)
            .where(ServiceTarget.target_id == target_id, Service.is_deleted.is_(False))
            .order_by(Service.id)
        )
        return list((await self._session.scalars(stmt)).all())

    async def is_target_in_use(self, target_id: int) -> bool:
        """타깃에 삭제되지 않은 서비스가 붙어 있거나, 지운 서비스라도 아직 내려가지 않았다.

        지운 서비스는 배포가 진행 중이거나(앱을 내리는 REMOVE 포함), GitOps 에 커밋한 release 가
        있는데 그 뒤로 성공한 REMOVE 가 없으면 서비스 디렉터리가 남아 있을 수 있어 쓰는 중으로 본다.
        """
        active_request = exists().where(
            DeploymentRequest.service_id == Service.id,
            DeploymentRequest.status.in_(ACTIVE_DEPLOYMENT_STATUSES),
        )
        live_release = exists().where(
            Release.service_id == Service.id,
            Release.target_id == target_id,
            Release.gitops_commit_sha.is_not(None),
            ~removed_after_release(),
        )
        stmt = select(
            exists().where(
                ServiceTarget.service_id == Service.id,
                ServiceTarget.target_id == target_id,
                or_(Service.is_deleted.is_(False), active_request, live_release),
            )
        )
        return bool(await self._session.scalar(stmt))

    async def save(self, service: Service) -> Service:
        self._session.add(service)
        await self._session.flush()
        return service

    async def replace_targets(self, service_id: int, target_ids: set[int]) -> None:
        await self._session.execute(
            delete(ServiceTarget).where(
                ServiceTarget.service_id == service_id,
                ServiceTarget.target_id.not_in(target_ids),
            )
        )
        if target_ids:
            await self._session.execute(
                insert(ServiceTarget)
                .values([{"service_id": service_id, "target_id": t} for t in target_ids])
                .on_conflict_do_nothing()
            )

    async def mark_as_deleted_by_project_id(self, project_id: int) -> None:
        """프로젝트 삭제 시 소속 서비스를 함께 소프트 삭제한다."""
        await self._session.execute(
            update(Service)
            .where(Service.project_id == project_id, Service.is_deleted.is_(False))
            .values(is_deleted=True, deleted_at=now_utc())
        )
