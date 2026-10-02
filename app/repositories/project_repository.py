from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import ReleaseStatus
from app.models.project import Project
from app.models.release import Release
from app.models.service import Service
from app.repositories.release_repository import removed_after_release


@dataclass(frozen=True)
class ServiceCounts:
    service_count: int
    online_service_count: int


class ProjectRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_id_and_owner_id(self, project_id: int, owner_id: int) -> Project | None:
        stmt = select(Project).where(
            Project.id == project_id, Project.owner_id == owner_id, Project.is_deleted.is_(False)
        )
        return (await self._session.scalars(stmt)).one_or_none()

    async def find_by_owner_id_and_name(self, owner_id: int, name: str) -> Project | None:
        stmt = select(Project).where(
            Project.owner_id == owner_id, Project.name == name, Project.is_deleted.is_(False)
        )
        return (await self._session.scalars(stmt)).one_or_none()

    async def search_by_owner_id(self, owner_id: int) -> list[Project]:
        stmt = (
            select(Project)
            .where(Project.owner_id == owner_id, Project.is_deleted.is_(False))
            .order_by(Project.created_at.desc(), Project.id.desc())
        )
        return list((await self._session.scalars(stmt)).all())

    async def count_services_by_project_ids(
        self, project_ids: list[int]
    ) -> dict[int, ServiceCounts]:
        """프로젝트별 서비스 수와 online 서비스 수.

        online 은 서비스의 가장 최근 릴리스가 성공했고 그 뒤에 서비스를 내리지(REMOVE) 않은 경우다.
        릴리스가 없으면 online 이 아니다.
        """
        latest_release = (
            select(Release.service_id, func.max(Release.id).label("release_id"))
            .group_by(Release.service_id)
            .subquery()
        )
        stmt = (
            select(
                Service.project_id,
                func.count(Service.id),
                func.count(Release.id).filter(
                    Release.status == ReleaseStatus.SUCCEEDED, ~removed_after_release()
                ),
            )
            .select_from(Service)
            .outerjoin(latest_release, latest_release.c.service_id == Service.id)
            .outerjoin(Release, Release.id == latest_release.c.release_id)
            .where(Service.project_id.in_(project_ids), Service.is_deleted.is_(False))
            .group_by(Service.project_id)
        )
        rows = (await self._session.execute(stmt)).all()
        return {project_id: ServiceCounts(total, online) for project_id, total, online in rows}

    async def save(self, project: Project) -> Project:
        self._session.add(project)
        await self._session.flush()
        return project
