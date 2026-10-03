from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.models.base import now_utc
from app.models.deployment_repair import DeploymentRepair


class DeploymentRepairRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

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

    async def claim_generation(self, repair_id: int) -> bool:
        statement = (
            update(DeploymentRepair)
            .where(
                DeploymentRepair.id == repair_id,
                DeploymentRepair.status == "RUNNING",
                DeploymentRepair.generation_started_at.is_(None),
            )
            .values(generation_started_at=now_utc())
            .returning(DeploymentRepair.id)
        )
        return (await self._session.scalars(statement)).one_or_none() is not None
