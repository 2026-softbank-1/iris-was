from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.deployment_status_history import DeploymentStatusHistory


class DeploymentStatusHistoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, history: DeploymentStatusHistory) -> DeploymentStatusHistory:
        self._session.add(history)
        await self._session.flush()
        return history

    async def search_by_deployment_request_id(
        self, deployment_request_id: int
    ) -> list[DeploymentStatusHistory]:
        """전이가 일어난 순서대로 돌려준다."""
        stmt = (
            select(DeploymentStatusHistory)
            .where(DeploymentStatusHistory.deployment_request_id == deployment_request_id)
            .order_by(DeploymentStatusHistory.created_at, DeploymentStatusHistory.id)
        )
        return list((await self._session.scalars(stmt)).all())
