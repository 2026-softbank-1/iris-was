"""Durable diagnosis queue with idempotency and lease-guarded terminal writes."""

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import DiagnosisJobStatus
from app.models.base import now_utc
from app.models.deployment_diagnosis import DeploymentDiagnosis


class DiagnosisRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_latest(self, deployment_id: int) -> DeploymentDiagnosis | None:
        statement = (
            select(DeploymentDiagnosis)
            .where(DeploymentDiagnosis.deployment_id == deployment_id)
            .order_by(DeploymentDiagnosis.created_at.desc(), DeploymentDiagnosis.id.desc())
            .limit(1)
        )
        return (await self._session.scalars(statement)).one_or_none()

    async def add_if_absent(self, row: DeploymentDiagnosis) -> DeploymentDiagnosis:
        statement = (
            insert(DeploymentDiagnosis)
            .values(
                id=row.id,
                service_id=row.service_id,
                deployment_id=row.deployment_id,
                requested_by=row.requested_by,
                attempt_id=row.attempt_id,
                trigger=row.trigger,
                status=row.status,
                stage=row.stage,
                model_selection=row.model_selection,
                deployment_context=row.deployment_context,
            )
            .on_conflict_do_nothing(index_elements=["deployment_id", "attempt_id"])
            .returning(DeploymentDiagnosis)
        )
        added = (await self._session.scalars(statement)).one_or_none()
        if added is not None:
            return added
        existing = (
            await self._session.scalars(
                select(DeploymentDiagnosis).where(
                    DeploymentDiagnosis.deployment_id == row.deployment_id,
                    DeploymentDiagnosis.attempt_id == row.attempt_id,
                )
            )
        ).one()
        return existing

    async def claim_next(self, lease_seconds: int) -> DeploymentDiagnosis | None:
        statement = (
            select(DeploymentDiagnosis)
            .where(
                or_(
                    DeploymentDiagnosis.status == DiagnosisJobStatus.QUEUED,
                    and_(
                        DeploymentDiagnosis.status == DiagnosisJobStatus.RUNNING,
                        DeploymentDiagnosis.locked_until <= func.clock_timestamp(),
                    ),
                )
            )
            .order_by(DeploymentDiagnosis.created_at, DeploymentDiagnosis.id)
            .limit(1)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        row = (await self._session.scalars(statement)).one_or_none()
        if row is None:
            return None
        row.status = DiagnosisJobStatus.RUNNING
        # Keep model_started across recovery; a previous remote generation may still run.
        if row.stage != "model_started":
            row.stage = "collecting_logs"
        row.attempts += 1
        row.lease_token = str(uuid4())
        row.locked_until = now_utc() + timedelta(seconds=lease_seconds)
        await self._session.flush()
        return row

    async def renew_lease(self, row_id: str, token: str, lease_seconds: int) -> bool:
        statement = (
            update(DeploymentDiagnosis)
            .where(
                DeploymentDiagnosis.id == row_id,
                DeploymentDiagnosis.lease_token == token,
                DeploymentDiagnosis.status == DiagnosisJobStatus.RUNNING,
                DeploymentDiagnosis.locked_until > func.clock_timestamp(),
            )
            .values(locked_until=func.clock_timestamp() + timedelta(seconds=lease_seconds))
            .returning(DeploymentDiagnosis.id)
        )
        return (await self._session.execute(statement)).scalar_one_or_none() is not None

    async def find_running(
        self, row_id: str, token: str, *, for_update: bool = False
    ) -> DeploymentDiagnosis | None:
        statement = select(DeploymentDiagnosis).where(
            DeploymentDiagnosis.id == row_id,
            DeploymentDiagnosis.lease_token == token,
            DeploymentDiagnosis.status == DiagnosisJobStatus.RUNNING,
            DeploymentDiagnosis.locked_until > func.clock_timestamp(),
        )
        if for_update:
            statement = statement.with_for_update()
        statement = statement.execution_options(populate_existing=True)
        return (await self._session.scalars(statement)).one_or_none()
