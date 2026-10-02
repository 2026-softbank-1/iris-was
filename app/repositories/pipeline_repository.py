from datetime import timedelta
from uuid import uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import PipelineStatus
from app.models.base import now_utc
from app.models.pipeline_run import PipelineRun

ACTIVE_PIPELINE_STATUSES = (
    PipelineStatus.ANALYZING,
    PipelineStatus.AWAITING_INPUT,
    PipelineStatus.PLANNING,
    PipelineStatus.BUILDING,
    PipelineStatus.DEPLOYING,
)


class PipelineRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, run: PipelineRun) -> PipelineRun:
        self._session.add(run)
        await self._session.flush()
        return run

    async def find_by_id(self, run_id: str, *, for_update: bool = False) -> PipelineRun | None:
        statement = select(PipelineRun).where(PipelineRun.id == run_id)
        if for_update:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return (await self._session.scalars(statement)).one_or_none()

    async def find_latest(self, service_id: int) -> PipelineRun | None:
        statement = (
            select(PipelineRun)
            .where(PipelineRun.service_id == service_id)
            .order_by(PipelineRun.created_at.desc(), PipelineRun.id.desc())
            .limit(1)
        )
        return (await self._session.scalars(statement)).one_or_none()

    async def find_by_key(self, key: str) -> PipelineRun | None:
        return (
            await self._session.scalars(
                select(PipelineRun).where(PipelineRun.idempotency_key == key)
            )
        ).one_or_none()

    async def find_active(self, service_id: int) -> PipelineRun | None:
        return (
            await self._session.scalars(
                select(PipelineRun).where(
                    PipelineRun.service_id == service_id,
                    PipelineRun.status.in_(ACTIVE_PIPELINE_STATUSES),
                )
            )
        ).one_or_none()

    async def claim_next(self, lease_seconds: int) -> PipelineRun | None:
        if lease_seconds <= 0:
            raise ValueError("pipeline lease must be positive")
        row = (
            await self._session.scalars(
                select(PipelineRun)
                .where(
                    PipelineRun.status.in_((PipelineStatus.QUEUED, *ACTIVE_PIPELINE_STATUSES)),
                    PipelineRun.status != PipelineStatus.AWAITING_INPUT,
                    or_(
                        PipelineRun.locked_until.is_(None),
                        PipelineRun.locked_until <= func.clock_timestamp(),
                    ),
                )
                .order_by(PipelineRun.updated_at, PipelineRun.id)
                .limit(1)
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
        ).one_or_none()
        if row is not None:
            row.lease_token = str(uuid4())
            row.locked_until = now_utc() + timedelta(seconds=lease_seconds)
            row.attempts += 1
            await self._session.flush()
        return row

    async def renew_lease(self, run_id: str, token: str, lease_seconds: int) -> bool:
        statement = (
            update(PipelineRun)
            .where(
                PipelineRun.id == run_id,
                PipelineRun.lease_token == token,
                PipelineRun.status.in_((PipelineStatus.QUEUED, *ACTIVE_PIPELINE_STATUSES)),
                PipelineRun.locked_until > func.clock_timestamp(),
            )
            .values(locked_until=func.clock_timestamp() + timedelta(seconds=lease_seconds))
            .returning(PipelineRun.id)
        )
        return (await self._session.execute(statement)).scalar_one_or_none() is not None

    async def find_owned_lease(self, run_id: str, token: str) -> PipelineRun | None:
        row = (
            await self._session.scalars(
                select(PipelineRun)
                .where(
                    PipelineRun.id == run_id,
                    PipelineRun.lease_token == token,
                    PipelineRun.locked_until > func.clock_timestamp(),
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).one_or_none()
        return row

    async def release_lease(self, run_id: str, token: str) -> None:
        await self._session.execute(
            update(PipelineRun)
            .where(
                PipelineRun.id == run_id,
                PipelineRun.lease_token == token,
            )
            .values(lease_token=None, locked_until=None)
        )
