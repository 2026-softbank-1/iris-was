"""Loki/Prometheus 응답을 서비스 관측 도메인 타입으로 변환한다."""

import json
import math
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx

from app.core.exceptions import ExternalError

MetricKind = Literal["cpu", "memory", "network_receive", "network_transmit"]


@dataclass(frozen=True)
class LogEntry:
    timestamp_ns: str
    message: str
    pod: str
    container: str


@dataclass(frozen=True)
class MetricPoint:
    timestamp: float
    value: float


@dataclass(frozen=True)
class MetricSeries:
    metric: MetricKind
    unit: str
    points: list[MetricPoint]


class ObservabilityClient(Protocol):
    async def search_logs(
        self,
        base_url: str,
        namespace: str,
        start_ns: int,
        end_ns: int,
        limit: int,
        search: str,
        direction: Literal["forward", "backward"] = "backward",
    ) -> list[LogEntry]: ...

    async def search_metrics(
        self,
        base_url: str,
        namespace: str,
        start: float,
        end: float,
        step: int,
    ) -> list[MetricSeries]: ...


class LokiPrometheusObservabilityClient:
    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self._http_client = http_client

    async def _get(self, url: str, params: dict[str, str]) -> dict[str, Any]:
        try:
            response = await self._http_client.get(url, params=params, timeout=10)
            response.raise_for_status()
            payload = response.json()
            if payload["status"] != "success" or not isinstance(payload["data"], dict):
                raise ValueError("unsuccessful query")
            return payload["data"]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise ExternalError("observability query failed") from exc

    async def search_logs(
        self,
        base_url: str,
        namespace: str,
        start_ns: int,
        end_ns: int,
        limit: int,
        search: str,
        direction: Literal["forward", "backward"] = "backward",
    ) -> list[LogEntry]:
        # namespace 는 인증된 서비스 ID 에서만 만든다. 사용자 검색은 문자열 리터럴로 인코딩한다.
        query = f'{{namespace={json.dumps(namespace)},container="app"}}'
        if search:
            query += f" |= {json.dumps(search, ensure_ascii=False)}"
        payload = await self._get(
            f"{base_url.rstrip('/')}/loki/api/v1/query_range",
            {
                "query": query,
                "start": str(start_ns),
                "end": str(end_ns),
                "limit": str(limit),
                "direction": direction,
            },
        )
        try:
            if payload["resultType"] != "streams":
                raise ValueError("expected streams")
            entries = []
            for stream in payload["result"]:
                labels = stream["stream"]
                for row in stream["values"]:
                    timestamp_ns, message = row[:2]
                    if not isinstance(timestamp_ns, str) or not isinstance(message, str):
                        raise ValueError("invalid log entry")
                    int(timestamp_ns)
                    if not all(
                        isinstance(labels.get(key, ""), str) for key in ("pod", "container")
                    ):
                        raise ValueError("invalid log labels")
                    entries.append(
                        LogEntry(
                            timestamp_ns,
                            message,
                            labels.get("pod", ""),
                            labels.get("container", ""),
                        )
                    )
            return sorted(
                entries, key=lambda entry: (int(entry.timestamp_ns), entry.pod, entry.message)
            )
        except (KeyError, TypeError, ValueError, IndexError, AttributeError) as exc:
            raise ExternalError("invalid log query response") from exc

    async def search_metrics(
        self,
        base_url: str,
        namespace: str,
        start: float,
        end: float,
        step: int,
    ) -> list[MetricSeries]:
        selector = f'namespace="{namespace}",container="app",image!=""'
        network = f'namespace="{namespace}",pod=~"app-.*",interface!="lo"'
        queries: list[tuple[MetricKind, str, str]] = [
            ("cpu", "cores", f"sum(rate(container_cpu_usage_seconds_total{{{selector}}}[5m]))"),
            ("memory", "bytes", f"sum(container_memory_working_set_bytes{{{selector}}})"),
            (
                "network_receive",
                "bytes/s",
                f"sum(rate(container_network_receive_bytes_total{{{network}}}[5m]))",
            ),
            (
                "network_transmit",
                "bytes/s",
                f"sum(rate(container_network_transmit_bytes_total{{{network}}}[5m]))",
            ),
        ]
        series = []
        for kind, unit, query in queries:
            payload = await self._get(
                f"{base_url.rstrip('/')}/api/v1/query_range",
                {
                    "query": query,
                    "start": str(start),
                    "end": str(end),
                    "step": str(step),
                },
            )
            try:
                if payload["resultType"] != "matrix" or len(payload["result"]) > 1:
                    raise ValueError("expected aggregate matrix")
                points = []
                for result in payload["result"]:
                    for timestamp, raw_value in result["values"]:
                        value = float(raw_value)
                        if math.isfinite(value):
                            points.append(MetricPoint(float(timestamp), value))
                series.append(MetricSeries(kind, unit, points))
            except (KeyError, TypeError, ValueError) as exc:
                raise ExternalError("invalid metric query response") from exc
        return series
