from sqlalchemy.ext.asyncio import AsyncSession

from app.models.console_session import ConsoleSession


class ConsoleSessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, console_session: ConsoleSession) -> ConsoleSession:
        self._session.add(console_session)
        await self._session.flush()
        return console_session
