from datetime import datetime

from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.onprem_metric_sample import OnpremMetricSample


class OnpremMetricSampleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_all(self, samples: list[OnpremMetricSample]) -> None:
        if not samples:
            return
        await self._session.execute(
            insert(OnpremMetricSample),
            [
                {
                    "service_id": sample.service_id,
                    "pod": sample.pod,
                    "collected_at": sample.collected_at,
                    "cpu_millicores": sample.cpu_millicores,
                    "memory_bytes": sample.memory_bytes,
                }
                for sample in samples
            ],
        )

    async def search_by_service_id(
        self, service_id: int, start: datetime, end: datetime
    ) -> list[OnpremMetricSample]:
        """[start, end] 안의 표본. 시각 순."""
        stmt = (
            select(OnpremMetricSample)
            .where(
                OnpremMetricSample.service_id == service_id,
                OnpremMetricSample.collected_at >= start,
                OnpremMetricSample.collected_at <= end,
            )
            .order_by(OnpremMetricSample.collected_at, OnpremMetricSample.id)
        )
        return list((await self._session.scalars(stmt)).all())

    async def delete_collected_before(self, cutoff: datetime) -> None:
        await self._session.execute(
            delete(OnpremMetricSample).where(OnpremMetricSample.collected_at < cutoff)
        )
