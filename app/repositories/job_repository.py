from datetime import timedelta
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.worker_exceptions import JobLeaseLostError
from app.enums import JobKind, JobStatus
from app.models.job import Job


class JobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, job: Job) -> Job:
        self._session.add(job)
        await self._session.flush()
        return job

    async def claim_next_job(
        self, worker_id: str, kinds: frozenset[JobKind], lease_seconds: int = 300
    ) -> Job | None:
        now = func.now()
        next_id = (
            select(Job.id)
            .where(
                Job.kind.in_(kinds),
                or_(
                    Job.status.in_((JobStatus.QUEUED, JobStatus.RETRY_WAIT))
                    & (Job.run_after <= now),
                    (Job.status == JobStatus.RUNNING) & (Job.locked_until < now),
                ),
            )
            .order_by(Job.priority.desc(), Job.created_at, Job.id)
            .limit(1)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )
        statement = (
            update(Job)
            .where(Job.id == next_id)
            .values(
                status=JobStatus.RUNNING,
                locked_by=worker_id,
                locked_until=now + timedelta(seconds=lease_seconds),
                attempts=Job.attempts + 1,
            )
            .returning(Job)
        )
        return (await self._session.scalars(statement)).one_or_none()

    async def get_owned_job(self, job: Job, worker_id: str) -> Job:
        """Attempts fence an old task even when the same process reclaims an expired lease."""
        statement = (
            select(Job)
            .where(*self._owned_conditions(job, worker_id))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        current = (await self._session.scalars(statement)).one_or_none()
        if current is None:
            raise JobLeaseLostError("job lease is no longer owned", job_id=job.id)
        return current

    async def renew_lease(self, job: Job, worker_id: str, lease_seconds: int = 300) -> bool:
        statement = (
            update(Job)
            .where(*self._owned_conditions(job, worker_id))
            .values(locked_until=func.now() + timedelta(seconds=lease_seconds))
            .returning(Job.id)
        )
        return (await self._session.scalars(statement)).one_or_none() is not None

    async def release(self, job: Job, worker_id: str, delay: timedelta = timedelta(0)) -> None:
        await self._update_owned(
            job,
            worker_id,
            status=JobStatus.QUEUED,
            run_after=func.now() + delay,
            attempts=Job.attempts - 1,
            locked_by=None,
            locked_until=None,
        )

    async def retry_later(self, job: Job, worker_id: str, error: str, delay: timedelta) -> None:
        await self._update_owned(
            job,
            worker_id,
            status=JobStatus.RETRY_WAIT,
            run_after=func.now() + delay,
            last_error=error[:1000],
            locked_by=None,
            locked_until=None,
        )

    async def mark_succeeded(self, job: Job, worker_id: str) -> None:
        await self._update_owned(
            job, worker_id, status=JobStatus.SUCCEEDED, locked_by=None, locked_until=None
        )

    async def mark_failed(self, job: Job, worker_id: str, error: str) -> None:
        await self._update_owned(
            job,
            worker_id,
            status=JobStatus.FAILED,
            last_error=error[:1000],
            locked_by=None,
            locked_until=None,
        )

    async def mark_manual_intervention(self, job: Job, worker_id: str, error: str) -> None:
        await self._update_owned(
            job,
            worker_id,
            status=JobStatus.MANUAL_INTERVENTION,
            last_error=error[:1000],
            locked_by=None,
            locked_until=None,
        )

    async def record_external_id(self, job: Job, worker_id: str, external_id: str) -> None:
        await self._update_owned(job, worker_id, external_id=external_id)

    @staticmethod
    def _owned_conditions(job: Job, worker_id: str) -> list[Any]:
        return [
            Job.id == job.id,
            Job.locked_by == worker_id,
            Job.attempts == job.attempts,
            Job.status == JobStatus.RUNNING,
            Job.locked_until > func.now(),
        ]

    async def _update_owned(self, job: Job, worker_id: str, **values: Any) -> None:
        statement = (
            update(Job)
            .where(*self._owned_conditions(job, worker_id))
            .values(**values)
            .returning(Job.id)
        )
        if (await self._session.scalars(statement)).one_or_none() is None:
            raise JobLeaseLostError("job lease is no longer owned", job_id=job.id)
