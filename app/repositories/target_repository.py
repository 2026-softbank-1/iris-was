from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.target import ServiceTarget, Target


class TargetRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def search_all(self) -> list[Target]:
        return list((await self._session.scalars(select(Target).order_by(Target.id))).all())

    async def search_by_ids(self, target_ids: list[int]) -> list[Target]:
        stmt = select(Target).where(Target.id.in_(target_ids)).order_by(Target.id)
        return list((await self._session.scalars(stmt)).all())

    async def search_by_service_id(self, service_id: int) -> list[Target]:
        statement = (
            select(Target)
            .join(ServiceTarget, ServiceTarget.target_id == Target.id)
            .where(ServiceTarget.service_id == service_id)
            .order_by(Target.id)
        )
        return list((await self._session.scalars(statement)).all())
