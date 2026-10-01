from sqlalchemy.ext.asyncio import AsyncSession

from app.models.job import Job


class JobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, job: Job) -> Job:
        self._session.add(job)
        await self._session.flush()
        return job
