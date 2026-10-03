from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.cli_login_session import CliLoginSession


class CliLoginSessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_public_id(self, public_id: str) -> CliLoginSession | None:
        stmt = select(CliLoginSession).where(CliLoginSession.public_id == public_id)
        return (await self._session.scalars(stmt)).one_or_none()

    async def find_by_public_id_for_update(self, public_id: str) -> CliLoginSession | None:
        """행을 잠가 읽는다. 승인·폴링이 같은 세션을 동시에 바꾸지 못하게 직렬화한다."""
        stmt = (
            select(CliLoginSession)
            .where(CliLoginSession.public_id == public_id)
            .with_for_update()
            # 이미 읽은 객체가 있어도 잠금을 잡은 시점의 값으로 새로 채운다.
            .execution_options(populate_existing=True)
        )
        return (await self._session.scalars(stmt)).one_or_none()

    async def save(self, cli_session: CliLoginSession) -> CliLoginSession:
        self._session.add(cli_session)
        await self._session.flush()
        return cli_session

    async def delete_expired_before(self, cutoff: datetime) -> None:
        stmt = delete(CliLoginSession).where(CliLoginSession.expires_at < cutoff)
        await self._session.execute(stmt)
