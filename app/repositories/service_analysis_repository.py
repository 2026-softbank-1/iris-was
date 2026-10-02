from datetime import timedelta
from uuid import uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import AnalysisJobStatus
from app.models.base import now_utc
from app.models.service_analysis import ServiceAnalysis


class ServiceAnalysisRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_latest(self, service_id: int) -> ServiceAnalysis | None:
        stmt = (
            select(ServiceAnalysis)
            .where(ServiceAnalysis.service_id == service_id)
            .order_by(ServiceAnalysis.created_at.desc(), ServiceAnalysis.id.desc())
            .limit(1)
        )
        return (await self._session.scalars(stmt)).one_or_none()

    async def find_by_id(
        self, analysis_id: str, service_id: int, *, for_update: bool = False
    ) -> ServiceAnalysis | None:
        stmt = select(ServiceAnalysis).where(
            ServiceAnalysis.id == analysis_id, ServiceAnalysis.service_id == service_id
        )
        if for_update:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return (await self._session.scalars(stmt)).one_or_none()

    async def find_active(self, service_id: int) -> ServiceAnalysis | None:
        stmt = select(ServiceAnalysis).where(
            ServiceAnalysis.service_id == service_id,
            ServiceAnalysis.status.in_((AnalysisJobStatus.QUEUED, AnalysisJobStatus.RUNNING)),
        )
        return (await self._session.scalars(stmt)).one_or_none()

    async def save(self, analysis: ServiceAnalysis) -> ServiceAnalysis:
        self._session.add(analysis)
        await self._session.flush()
        return analysis

    async def claim_next(self, lease_seconds: int) -> ServiceAnalysis | None:
        self._validate_lease_seconds(lease_seconds)
        stmt = (
            select(ServiceAnalysis)
            .where(
                or_(
                    ServiceAnalysis.status == AnalysisJobStatus.QUEUED,
                    and_(
                        ServiceAnalysis.status == AnalysisJobStatus.RUNNING,
                        ServiceAnalysis.locked_until <= func.clock_timestamp(),
                    ),
                )
            )
            .order_by(ServiceAnalysis.created_at, ServiceAnalysis.id)
            .limit(1)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        analysis = (await self._session.scalars(stmt)).one_or_none()
        if analysis is None:
            return None
        claimed_at = now_utc()
        analysis.status = AnalysisJobStatus.RUNNING
        analysis.stage = "snapshot"
        analysis.attempts += 1
        analysis.lease_token = str(uuid4())
        analysis.locked_until = claimed_at + timedelta(seconds=lease_seconds)
        await self._session.flush()
        return analysis

    async def renew_lease(self, analysis_id: str, lease_token: str, lease_seconds: int) -> bool:
        self._validate_lease_seconds(lease_seconds)
        stmt = (
            update(ServiceAnalysis)
            .where(
                ServiceAnalysis.id == analysis_id,
                ServiceAnalysis.lease_token == lease_token,
                ServiceAnalysis.status == AnalysisJobStatus.RUNNING,
                ServiceAnalysis.locked_until > func.clock_timestamp(),
            )
            .values(locked_until=func.clock_timestamp() + timedelta(seconds=lease_seconds))
            .returning(ServiceAnalysis.id)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none() is not None

    async def find_running(
        self, analysis_id: str, lease_token: str, *, for_update: bool = False
    ) -> ServiceAnalysis | None:
        stmt = select(ServiceAnalysis).where(
            ServiceAnalysis.id == analysis_id,
            ServiceAnalysis.lease_token == lease_token,
            ServiceAnalysis.status == AnalysisJobStatus.RUNNING,
            ServiceAnalysis.locked_until > func.clock_timestamp(),
        )
        if for_update:
            stmt = stmt.with_for_update()
        stmt = stmt.execution_options(populate_existing=True)
        analysis = (await self._session.scalars(stmt)).one_or_none()
        if analysis is not None and (
            analysis.locked_until is None or analysis.locked_until <= now_utc()
        ):
            return None
        return analysis

    @staticmethod
    def _validate_lease_seconds(lease_seconds: int) -> None:
        if type(lease_seconds) is not int or lease_seconds <= 0:
            raise ValueError("analysis lease must be a positive number of seconds")
