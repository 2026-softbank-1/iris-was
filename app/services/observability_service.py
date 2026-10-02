import asyncio
import json
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from app.clients.observability_client import LogEntry, MetricSeries, ObservabilityClient
from app.core.config import ObservabilityEndpoint
from app.core.exceptions import (
    ExternalError,
    InvalidInputError,
    NotConfiguredError,
    ServiceNotFoundError,
)
from app.repositories.service_repository import ServiceRepository


class ObservabilityService:
    def __init__(
        self,
        service_repository: ServiceRepository,
        client: ObservabilityClient,
        endpoints: dict[int, ObservabilityEndpoint],
    ) -> None:
        self._service_repository = service_repository
        self._client = client
        self._endpoints = endpoints

    async def get_scope(self, owner_id: int, service_id: int, target_id: int) -> str:
        service = await self._service_repository.find_by_id_and_owner_id(service_id, owner_id)
        if service is None:
            raise ServiceNotFoundError("service not found", service_id=service_id)
        targets = await self._service_repository.search_target_ids_by_service_ids([service_id])
        if target_id not in targets[service_id]:
            raise InvalidInputError("target is not assigned to this service", target_id=target_id)
        return f"svc-{service_id}"

    def _get_url(self, target_id: int, kind: str) -> str:
        endpoint = self._endpoints.get(target_id)
        url = None if endpoint is None else getattr(endpoint, kind)
        if url is None:
            raise NotConfiguredError("observability backend is not configured", target_id=target_id)
        return str(url)

    @staticmethod
    def validate_range(start: datetime, end: datetime) -> None:
        if start.tzinfo is None or end.tzinfo is None:
            raise InvalidInputError("start and end must include a timezone")
        if start >= end or (end - start).total_seconds() > 7 * 86400:
            raise InvalidInputError("time range must be positive and at most 7 days")
        if end > datetime.now(UTC):
            raise InvalidInputError("end cannot be in the future")

    async def search_logs(
        self, target_id: int, namespace: str, start_ns: int, end_ns: int, limit: int, search: str
    ) -> list[LogEntry]:
        return await self._client.search_logs(
            self._get_url(target_id, "loki_url"), namespace, start_ns, end_ns, limit, search
        )

    async def search_metrics(
        self, target_id: int, namespace: str, start: datetime, end: datetime, step: int
    ) -> list[MetricSeries]:
        if (end - start).total_seconds() / step > 1440:
            raise InvalidInputError("range and step may produce at most 1440 points per metric")
        return await self._client.search_metrics(
            self._get_url(target_id, "prometheus_url"),
            namespace,
            start.timestamp(),
            end.timestamp(),
            step,
        )

    async def prepare_stream(
        self, target_id: int, namespace: str, start_ns: int, end_ns: int, search: str
    ) -> list[LogEntry]:
        return await self._client.search_logs(
            self._get_url(target_id, "loki_url"),
            namespace,
            start_ns,
            end_ns,
            1000,
            search,
            "forward",
        )

    async def stream_logs(
        self, target_id: int, namespace: str, cursor: int, search: str, initial: list[LogEntry]
    ) -> AsyncIterator[str]:
        # DB 세션은 라우터에서 연결 시작 전에 종료한다. 스트림은 최대 5분 뒤 재연결한다.
        deadline = time.monotonic() + 300
        lower_bound = cursor
        entries = initial
        seen: set[LogEntry] = set()
        while True:
            if len(entries) >= 1000:
                yield 'event: overflow\ndata: {"code":"LOG_STREAM_OVERFLOW"}\n\n'
                return
            entries = [entry for entry in entries if entry not in seen]
            if entries:
                cursor = max(cursor, max(int(entry.timestamp_ns) for entry in entries) + 1)
                seen.update(entries)
                seen = {entry for entry in seen if int(entry.timestamp_ns) >= cursor - 10 * 10**9}
                payload = [
                    {
                        "timestampNs": e.timestamp_ns,
                        "message": e.message,
                        "pod": e.pod,
                        "container": e.container,
                    }
                    for e in entries
                ]
                yield f"id: {cursor}\nevent: logs\ndata: {json.dumps(payload)}\n\n"
            else:
                yield ": heartbeat\n\n"
            if time.monotonic() >= deadline:
                return
            await asyncio.sleep(2)
            try:
                entries = await self._client.search_logs(
                    self._get_url(target_id, "loki_url"),
                    namespace,
                    max(lower_bound, cursor - 10 * 10**9),
                    time.time_ns(),
                    1000,
                    search,
                    "forward",
                )
            except ExternalError:
                yield 'event: error\ndata: {"code":"EXTERNAL_ERROR"}\n\n'
                return
