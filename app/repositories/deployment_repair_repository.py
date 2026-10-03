from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError
from app.models.base import now_utc
from app.models.deployment_repair import DeploymentRepair
from app.models.project import Project
from app.models.service import Service


class DeploymentRepairRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def pending_automatic(self) -> list[DeploymentRepair]:
        return list(
            await self._session.scalars(
                select(DeploymentRepair)
                .join(Service, Service.id == DeploymentRepair.service_id)
                .join(Project, Project.id == Service.project_id)
                .where(
                    Project.owner_id == DeploymentRepair.requested_by,
                    Service.is_deleted.is_(False),
                    Project.is_deleted.is_(False),
                    DeploymentRepair.request_metadata["autoMerge"].as_boolean().is_(True),
                    DeploymentRepair.status.in_(["RUNNING", "UNKNOWN_OUTCOME", "SUCCEEDED"]),
                    DeploymentRepair.request_metadata["publication"]["status"]
                    .as_string()
                    .not_in(["MERGED", "ERROR", "SKIPPED"]),
                )
                .order_by(DeploymentRepair.id)
                .limit(8)
            )
        )

    async def latest(
        self, service_id: int, deployment_id: int, diagnosis_id: int
    ) -> DeploymentRepair:
        row = (
            await self._session.scalars(
                select(DeploymentRepair)
                .where(
                    DeploymentRepair.service_id == service_id,
                    DeploymentRepair.deployment_request_id == deployment_id,
                    DeploymentRepair.diagnosis_id == diagnosis_id,
                )
                .order_by(DeploymentRepair.id.desc())
                .limit(1)
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError("repair not found")
        return row

    async def lock_publication(self, repair_id: int) -> DeploymentRepair:
        try:
            return (
                await self._session.scalars(
                    select(DeploymentRepair)
                    .where(DeploymentRepair.id == repair_id)
                    .with_for_update(nowait=True)
                    .execution_options(populate_existing=True)
                )
            ).one()
        except DBAPIError as exc:
            await self._session.rollback()
            if getattr(exc.orig, "sqlstate", None) == "55P03":
                raise ConflictError(
                    "repair publication is already running", publication_busy=True
                ) from None
            raise

    async def find_by_service_id_and_key(
        self, service_id: int, key: str
    ) -> DeploymentRepair | None:
        return (
            await self._session.scalars(
                select(DeploymentRepair).where(
                    DeploymentRepair.service_id == service_id,
                    DeploymentRepair.idempotency_key == key,
                )
            )
        ).one_or_none()

    async def find_running_by_deployment_request_id(
        self, deployment_request_id: int
    ) -> DeploymentRepair | None:
        return (
            await self._session.scalars(
                select(DeploymentRepair).where(
                    DeploymentRepair.deployment_request_id == deployment_request_id,
                    DeploymentRepair.status == "RUNNING",
                )
            )
        ).one_or_none()

    async def get_by_id(self, repair_id: int) -> DeploymentRepair:
        row = (
            await self._session.scalars(
                select(DeploymentRepair)
                .where(DeploymentRepair.id == repair_id)
                .execution_options(populate_existing=True)
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError("repair not found", repair_id=repair_id)
        return row

    async def add_running_if_absent(self, values: dict[str, Any]) -> DeploymentRepair | None:
        statement = (
            insert(DeploymentRepair)
            .values(**values, status="RUNNING")
            .on_conflict_do_nothing()
            .returning(DeploymentRepair)
        )
        return (await self._session.scalars(statement)).one_or_none()

    async def claim_generation(
        self, repair_id: int, *, deadline_at: datetime | None = None
    ) -> bool:
        statement = (
            update(DeploymentRepair)
            .where(
                DeploymentRepair.id == repair_id,
                DeploymentRepair.status == "RUNNING",
                DeploymentRepair.generation_started_at.is_(None),
            )
            .values(
                generation_started_at=now_utc(),
                **({"deadline_at": deadline_at} if deadline_at is not None else {}),
            )
            .returning(DeploymentRepair.id)
        )
        return (await self._session.scalars(statement)).one_or_none() is not None
