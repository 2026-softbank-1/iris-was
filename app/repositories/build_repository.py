from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.models.build import Build


class BuildRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_deployment_request_id(self, deployment_request_id: int) -> Build | None:
        statement = select(Build).where(Build.deployment_request_id == deployment_request_id)
        return (await self._session.scalars(statement)).one_or_none()

    async def get_by_id(self, build_id: int, *, for_update: bool = False) -> Build:
        statement = select(Build).where(Build.id == build_id)
        if for_update:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        build = (await self._session.scalars(statement)).one_or_none()
        if build is None:
            raise NotFoundError("build not found", build_id=build_id)
        return build

    async def save(self, build: Build) -> Build:
        self._session.add(build)
        await self._session.flush()
        return build
