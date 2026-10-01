from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_id(self, user_id: int) -> User | None:
        return await self._session.get(User, user_id)

    async def find_by_github_id(self, github_id: int) -> User | None:
        stmt = select(User).where(User.github_id == github_id)
        return (await self._session.scalars(stmt)).one_or_none()

    async def save(self, user: User) -> User:
        self._session.add(user)
        await self._session.flush()
        return user
