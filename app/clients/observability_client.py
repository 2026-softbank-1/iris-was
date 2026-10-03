"""Loki/Prometheus 응답을 서비스 관측 도메인 타입으로 변환한다."""

import json
import math
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx

from app.core.exceptions import ExternalError

MetricKind = Literal["cpu", "memory", "network_receive", "network_transmit"]
# total: 서비스 전체 합계 1개, pod: Pod(replica)별 시리즈
MetricGrouping = Literal["total", "pod"]
StatusClass = Literal["2xx", "3xx", "4xx", "5xx"]

# OTel 수집기의 리소스 속성(k8s.namespace.name 등)을 Loki·Prometheus 가 `_` 로 바꾼 라벨이다.
NAMESPACE_LABEL = "k8s_namespace_name"
POD_LABEL = "k8s_pod_name"
CONTAINER_LABEL = "k8s_container_name"
# Deploy Worker 가 Pod annotation 으로 넣은 release.id 를 수집기가 라벨로 올린다.
RELEASE_LABEL = "iris_release_id"
# iris-infra 의 ALB 접근 로그 수집기가 정규화해 보내는 스트림이다(job·k8s_namespace_name 라벨).
ALB_ACCESS_JOB = "iris-alb-access"
_STATUS_CLASS_RANGES: dict[StatusClass, tuple[int, int]] = {
    "2xx": (200, 299),
    "3xx": (300, 399),
    "4xx": (400, 499),
    "5xx": (500, 599),
}


@dataclass(frozen=True)
class LogEntry:
    timestamp_ns: str
    message: str
    pod: str
    container: str


@dataclass(frozen=True)
class NetworkLogEntry:
    """ALB 가 완료한 요청 1건. 수집기가 URL·메서드·IP·User-Agent 를 보내지 않아 이 값들뿐이다."""

    timestamp_ns: str
    status: int
    target_status: int | None
    received_bytes: int
    sent_bytes: int
    response_time_seconds: float | None


@dataclass(frozen=True)
class MetricPoint:
    timestamp: float
    value: float


@dataclass(frozen=True)
class MetricSeries:
    metric: MetricKind
    unit: str
    points: list[MetricPoint]
    pod: str | None = None


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
        *,
        release_ids: list[int] | None = None,
    ) -> list[LogEntry]: ...

    async def search_network_logs(
        self,
        base_url: str,
        namespace: str,
        start_ns: int,
        end_ns: int,
        limit: int,
        status_class: StatusClass | None,
    ) -> list[NetworkLogEntry]: ...

    async def search_metrics(
        self,
        base_url: str,
        namespace: str,
        start: float,
        end: float,
        step: int,
        group_by: MetricGrouping = "total",
    ) -> list[MetricSeries]: ...


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_network_entry(timestamp_ns: str, line: str) -> NetworkLogEntry | None:
    """수집기가 만든 JSON 본문을 항목으로 바꾼다.

    형식이 다른 줄은 화면에 보일 값이 없고 한 줄 때문에 나머지를 잃지 않도록 건너뛴다.
    TargetGroup ARN·record id 같은 내부 값은 항목에 담지 않는다.
    """
    try:
        body = json.loads(line)
        status = body["elb_status_code"]
        received = body["received_bytes"]
        sent = body["sent_bytes"]
    except (ValueError, KeyError, TypeError):
        return None
    target_status = body.get("target_status_code")
    latency = body.get("target_processing_time")
    if not (_is_int(status) and _is_int(received) and _is_int(sent)):
        return None
    if target_status is not None and not _is_int(target_status):
        return None
    if latency is not None and (isinstance(latency, bool) or not isinstance(latency, int | float)):
        return None
    return NetworkLogEntry(
        timestamp_ns,
        status,
        target_status,
        received,
        sent,
        float(latency) if latency is not None else None,
    )


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
        *,
        release_ids: list[int] | None = None,
    ) -> list[LogEntry]:
        # namespace 는 인증된 서비스 ID 에서만 만든다. 사용자 검색은 문자열 리터럴로 인코딩한다.
        selector = f'{NAMESPACE_LABEL}={json.dumps(namespace)},{CONTAINER_LABEL}="app"'
        if release_ids:
            # release id 는 DB 의 정수라 정규식에 넣어도 안전하다.
            selector += f',{RELEASE_LABEL}=~"{"|".join(str(i) for i in release_ids)}"'
        query = f"{{{selector}}}"
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
                        isinstance(labels.get(key, ""), str) for key in (POD_LABEL, CONTAINER_LABEL)
                    ):
                        raise ValueError("invalid log labels")
                    entries.append(
                        LogEntry(
                            timestamp_ns,
                            message,
                            labels.get(POD_LABEL, ""),
                            labels.get(CONTAINER_LABEL, ""),
                        )
                    )
            return sorted(
                entries, key=lambda entry: (int(entry.timestamp_ns), entry.pod, entry.message)
            )
        except (KeyError, TypeError, ValueError, IndexError, AttributeError) as exc:
            raise ExternalError("invalid log query response") from exc

    async def search_network_logs(
        self,
        base_url: str,
        namespace: str,
        start_ns: int,
        end_ns: int,
        limit: int,
        status_class: StatusClass | None,
    ) -> list[NetworkLogEntry]:
        query = f'{{job="{ALB_ACCESS_JOB}",{NAMESPACE_LABEL}={json.dumps(namespace)}}}'
        if status_class is not None:
            low, high = _STATUS_CLASS_RANGES[status_class]
            query += (
                f' | json | __error__ = "" | elb_status_code >= {low} | elb_status_code <= {high}'
            )
        payload = await self._get(
            f"{base_url.rstrip('/')}/loki/api/v1/query_range",
            {
                "query": query,
                "start": str(start_ns),
                "end": str(end_ns),
                "limit": str(limit),
                "direction": "backward",
            },
        )
        try:
            if payload["resultType"] != "streams":
                raise ValueError("expected streams")
            entries = []
            for stream in payload["result"]:
                for row in stream["values"]:
                    timestamp_ns, line = row[:2]
                    if not isinstance(timestamp_ns, str) or not isinstance(line, str):
                        raise ValueError("invalid log entry")
                    int(timestamp_ns)
                    entry = _parse_network_entry(timestamp_ns, line)
                    if entry is not None:
                        entries.append(entry)
            return sorted(entries, key=lambda entry: int(entry.timestamp_ns))
        except (KeyError, TypeError, ValueError, IndexError, AttributeError) as exc:
            raise ExternalError("invalid network log query response") from exc

    async def search_metrics(
        self,
        base_url: str,
        namespace: str,
        start: float,
        end: float,
        step: int,
        group_by: MetricGrouping = "total",
    ) -> list[MetricSeries]:
        # 수집기는 svc-* Pod 의 kubeletstats 메트릭 3종만 보낸다.
        # Pod 단위지만 iris-service Pod 는 컨테이너가 하나라 값이 같다.
        selector = f'{NAMESPACE_LABEL}="{namespace}"'
        network_io = "k8s_pod_network_io_bytes_total"
        # pod 는 Pod 라벨별로 나눠 집계한다. 라벨 이름은 상수라 사용자 입력이 들어가지 않는다.
        aggregate = "sum" if group_by == "total" else f"sum by ({POD_LABEL}) "
        cpu_time = "k8s_pod_cpu_time_seconds_total"
        queries: list[tuple[MetricKind, str, str]] = [
            ("cpu", "cores", f"{aggregate}(rate({cpu_time}{{{selector}}}[5m]))"),
            ("memory", "bytes", f"{aggregate}(k8s_pod_memory_working_set_bytes{{{selector}}})"),
            (
                "network_receive",
                "bytes/s",
                f'{aggregate}(rate({network_io}{{{selector},direction="receive"}}[5m]))',
            ),
            (
                "network_transmit",
                "bytes/s",
                f'{aggregate}(rate({network_io}{{{selector},direction="transmit"}}[5m]))',
            ),
        ]
        series: list[MetricSeries] = []
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
                if payload["resultType"] != "matrix":
                    raise ValueError("expected matrix")
                results = payload["result"]
                if group_by == "total":
                    if len(results) > 1:
                        raise ValueError("expected aggregate matrix")
                    # 데이터가 없어도 metric 자리는 비워 두지 않고 빈 points 로 유지한다.
                    series.append(
                        MetricSeries(kind, unit, _parse_points(results[0]) if results else [])
                    )
                else:
                    by_pod = [
                        MetricSeries(kind, unit, _parse_points(result), _parse_pod(result))
                        for result in results
                    ]
                    series.extend(sorted(by_pod, key=lambda item: item.pod or ""))
            except (KeyError, TypeError, ValueError) as exc:
                raise ExternalError("invalid metric query response") from exc
        return series


def _parse_points(result: dict[str, Any]) -> list[MetricPoint]:
    points = []
    for timestamp, raw_value in result["values"]:
        value = float(raw_value)
        if math.isfinite(value):
            points.append(MetricPoint(float(timestamp), value))
    return points


def _parse_pod(result: dict[str, Any]) -> str:
    pod = result["metric"][POD_LABEL]
    if not isinstance(pod, str) or not pod:
        raise ValueError("invalid pod label")
    return pod
