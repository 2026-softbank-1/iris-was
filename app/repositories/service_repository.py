from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import now_utc
from app.models.project import Project
from app.models.service import Service
from app.models.target import ServiceTarget


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

    async def get_scaling_config_for_update(self, service_id: int) -> dict[str, Any] | None:
        """원하는 설정의 최신 값을 읽고 배포 요청이 커밋될 때까지 서비스 행을 잠근다."""
        # PUT이 같은 트랜잭션에서 바꾼 값도 DB에서 읽을 수 있도록 먼저 반영한다.
        await self._session.flush()
        stmt = (
            select(Service.scaling_config)
            .where(Service.id == service_id)
            .with_for_update(of=Service)
        )
        return (await self._session.scalars(stmt)).one()

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
