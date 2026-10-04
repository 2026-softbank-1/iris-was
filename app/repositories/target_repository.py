from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.target import Target


def _visible_to(owner_id: int) -> ColumnElement[bool]:
    """공용 타깃(소유자 없음)과 이 사용자가 등록한 서버의 타깃. 삭제된 타깃은 뺀다."""
    return Target.is_deleted.is_(False) & or_(
        Target.owner_id.is_(None), Target.owner_id == owner_id
    )


class TargetRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def search_all(self) -> list[Target]:
        return list((await self._session.scalars(select(Target).order_by(Target.id))).all())

    async def search_by_ids(self, target_ids: list[int]) -> list[Target]:
        """삭제된 타깃도 돌려준다(배포 이력이 가리킨다). 등록한 서버를 함께 읽는다."""
        stmt = (
            select(Target)
            .options(selectinload(Target.onprem_server))
            .where(Target.id.in_(target_ids))
            .order_by(Target.id)
        )
        return list((await self._session.scalars(stmt)).all())

    async def search_visible(self, owner_id: int) -> list[Target]:
        """사용자가 고를 수 있는 타깃. 등록한 서버를 함께 읽는다."""
        stmt = (
            select(Target)
            .options(selectinload(Target.onprem_server))
            .where(_visible_to(owner_id))
            .order_by(Target.id)
        )
        return list((await self._session.scalars(stmt)).all())

    async def search_visible_by_ids(self, target_ids: list[int], owner_id: int) -> list[Target]:
        """target_ids 중 사용자가 고를 수 있는 것만. 남의 서버 타깃은 없는 타깃과 같다."""
        stmt = (
            select(Target)
            .where(Target.id.in_(target_ids), _visible_to(owner_id))
            .order_by(Target.id)
        )
        return list((await self._session.scalars(stmt)).all())

    async def add(self, target: Target) -> Target:
        self._session.add(target)
        await self._session.flush()
        return target
