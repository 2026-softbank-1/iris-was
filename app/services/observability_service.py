import asyncio
import json
import math
import time
from collections import Counter, defaultdict
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Literal

from pydantic import HttpUrl

from app.clients.argocd_client import PodLogClient
from app.clients.observability_client import (
    LogEntry,
    MetricGrouping,
    MetricKind,
    MetricPoint,
    MetricSeries,
    NetworkLogEntry,
    ObservabilityClient,
    StatusClass,
    TrafficMetrics,
)
from app.core.config import DEFAULT_TRAFFIC_CLUSTER
from app.core.exceptions import (
    ExternalError,
    InvalidInputError,
    NotConfiguredError,
    ServiceNotFoundError,
)
from app.enums import TargetKind
from app.models.onprem_metric_sample import OnpremMetricSample
from app.repositories.onprem_metric_sample_repository import OnpremMetricSampleRepository
from app.repositories.service_repository import ServiceRepository
from app.repositories.target_repository import TargetRepository

MAX_POD_SERIES = 50
# 앱 컨테이너 이름(iris-service chart). Loki 조회도 같은 컨테이너만 본다.
APP_CONTAINER = "app"
# on-prem 로그를 Argo CD 에서 읽을 때 Pod 마다 가져오는 최대 줄 수. 시각·검색은 받은 뒤 거른다.
ONPREM_TAIL_LINES = 5000
# Prometheus 경로와 같은 metric 이름·단위. 등록한 서버는 CPU·메모리만 보내 네트워크는 비어 있다.
_SERVER_METRICS: tuple[tuple[MetricKind, str], ...] = (
    ("cpu", "cores"),
    ("memory", "bytes"),
    ("network_receive", "bytes/s"),
    ("network_transmit", "bytes/s"),
)


def bucket_server_metrics(
    samples: list[OnpremMetricSample],
    start: datetime,
    step: int,
    group_by: MetricGrouping,
) -> list[MetricSeries]:
    """서버가 보낸 표본을 step 칸으로 묶어 Prometheus 경로와 같은 모양의 시리즈로 만든다.

    칸마다 Pod 별 평균을 내고, total 은 그 평균을 Pod 끼리 더한다. 칸의 시각은 칸이 시작하는
    시각(start + i·step)이고, 표본이 없는 칸은 점을 두지 않는다. CPU 는 millicores 를 cores 로
    바꾼다.
    """
    origin = start.timestamp()
    # (pod, 칸) → [cpu 합, memory 합, 개수]
    sums: dict[tuple[str, int], list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])
    for sample in samples:
        bucket = int((sample.collected_at.timestamp() - origin) // step)
        acc = sums[(sample.pod, bucket)]
        acc[0] += sample.cpu_millicores / 1000
        acc[1] += sample.memory_bytes
        acc[2] += 1
    averages = {key: (cpu / count, memory / count) for key, (cpu, memory, count) in sums.items()}

    def points(values: dict[int, float]) -> list[MetricPoint]:
        return [MetricPoint(origin + bucket * step, values[bucket]) for bucket in sorted(values)]

    series: list[MetricSeries] = []
    for index, (kind, unit) in enumerate(_SERVER_METRICS):
        if group_by == "total":
            totals: dict[int, float] = defaultdict(float)
            if index < 2:
                for (_, bucket), values in averages.items():
                    totals[bucket] += values[index]
            series.append(MetricSeries(kind, unit, points(totals)))
            continue
        if index >= 2:
            continue
        by_pod: dict[str, dict[int, float]] = defaultdict(dict)
        for (pod, bucket), values in averages.items():
            by_pod[pod][bucket] = values[index]
        series.extend(MetricSeries(kind, unit, points(by_pod[pod]), pod) for pod in sorted(by_pod))
    return series


class ObservabilityService:
    def __init__(
        self,
        service_repository: ServiceRepository,
        client: ObservabilityClient,
        loki_url: HttpUrl | None,
        prometheus_url: HttpUrl | None,
        traffic_cluster: str = DEFAULT_TRAFFIC_CLUSTER,
        *,
        target_repository: TargetRepository | None = None,
        pod_log_client: PodLogClient | None = None,
        metric_sample_repository: OnpremMetricSampleRepository | None = None,
    ) -> None:
        self._service_repository = service_repository
        self._client = client
        self._urls = {"loki_url": loki_url, "prometheus_url": prometheus_url}
        self._traffic_cluster = traffic_cluster
        self._target_repository = target_repository
        self._pod_log_client = pod_log_client
        # 사용자가 등록한 서버가 보낸 메트릭 표본. 그 서버 타깃의 메트릭만 여기서 읽는다.
        self._metric_sample_repository = metric_sample_repository
        # DB 를 읽는 단계(get_scope·get_target_kind)에서 채운다. 외부 조회 단계는 DB 를 읽지 않는다.
        self._target_kinds: dict[int, TargetKind] = {}
        # 사용자가 등록한 서버의 타깃 id. 메트릭을 서버가 보낸 표본에서 읽는다.
        self._server_target_ids: set[int] = set()

    async def get_scope(self, owner_id: int, service_id: int, target_id: int) -> str:
        service = await self._service_repository.find_by_id_and_owner_id(service_id, owner_id)
        if service is None:
            raise ServiceNotFoundError("service not found", service_id=service_id)
        targets = await self._service_repository.search_target_ids_by_service_ids([service_id])
        if target_id not in targets[service_id]:
            raise InvalidInputError("target is not assigned to this service", target_id=target_id)
        await self.get_target_kind(target_id)
        return self.build_namespace(service_id)

    async def get_target_kind(self, target_id: int) -> TargetKind:
        """로그·메트릭을 어디서 읽을지 정하는 타깃 종류. 조회 전에 DB 세션이 열려 있을 때 부른다.

        on-prem 타깃은 Loki·Prometheus 수집 대상이 아니라 런타임 로그만 Argo CD 로 읽는다.
        """
        kind = self._target_kinds.get(target_id)
        if kind is not None:
            return kind
        kind = TargetKind.AWS
        if self._target_repository is not None:
            targets = await self._target_repository.search_by_ids([target_id])
            if targets:
                kind = targets[0].kind
                if targets[0].onprem_server is not None:
                    self._server_target_ids.add(target_id)
        self._target_kinds[target_id] = kind
        return kind

    def _is_onprem(self, target_id: int) -> bool:
        return self._target_kinds.get(target_id) == TargetKind.ONPREM

    def _require_collected_target(self, target_id: int) -> None:
        if self._is_onprem(target_id):
            raise NotConfiguredError(
                "metrics and network logs are not available for on-prem targets",
                target_id=target_id,
                target_kind=TargetKind.ONPREM.value,
            )

    @staticmethod
    def build_namespace(service_id: int) -> str:
        """서비스가 배포되는 Kubernetes namespace. 로그·메트릭의 서비스 구분 라벨 값이다."""
        return f"svc-{service_id}"

    def _get_url(self, target_id: int, kind: str) -> str:
        url = self._urls[kind]
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
        self,
        target_id: int,
        namespace: str,
        start_ns: int,
        end_ns: int,
        limit: int,
        search: str,
        release_ids: list[int] | None = None,
    ) -> list[LogEntry]:
        if self._is_onprem(target_id):
            # Pod 로그엔 release 구분이 없어 지금 Pod 들의 로그를 시각으로만 거른다.
            return await self._search_onprem_logs(
                target_id, namespace, start_ns, end_ns, limit, search, "backward"
            )
        url = self._get_url(target_id, "loki_url")
        if release_ids is None:
            return await self._client.search_logs(url, namespace, start_ns, end_ns, limit, search)
        return await self._client.search_logs(
            url, namespace, start_ns, end_ns, limit, search, release_ids=release_ids
        )

    async def search_network_logs(
        self,
        target_id: int,
        namespace: str,
        start_ns: int,
        end_ns: int,
        limit: int,
        status_class: StatusClass | None,
    ) -> list[NetworkLogEntry]:
        self._require_collected_target(target_id)
        return await self._client.search_network_logs(
            self._get_url(target_id, "loki_url"),
            namespace,
            start_ns,
            end_ns,
            limit,
            status_class,
        )

    async def search_metrics(
        self,
        target_id: int,
        namespace: str,
        start: datetime,
        end: datetime,
        step: int,
        group_by: MetricGrouping = "total",
    ) -> list[MetricSeries]:
        """CPU·메모리·네트워크 시계열. 사용자가 등록한 서버는 서버가 보낸 표본(CPU·메모리)에서,
        그 밖의 on-prem 타깃은 503, AWS 는 Prometheus 에서 읽는다.
        """
        if target_id not in self._server_target_ids:
            self._require_collected_target(target_id)
        if (end - start).total_seconds() / step > 1440:
            raise InvalidInputError("range and step may produce at most 1440 points per metric")
        if target_id in self._server_target_ids:
            series = await self._search_server_metrics(namespace, start, end, step, group_by)
        else:
            series = await self._client.search_metrics(
                self._get_url(target_id, "prometheus_url"),
                namespace,
                start.timestamp(),
                end.timestamp(),
                step,
                group_by,
            )
        # 롤링 배포가 잦은 긴 범위는 Pod 이름이 바뀌며 시리즈가 늘어난다. 잘라내지 않고 거절한다.
        if group_by == "pod":
            pods_per_metric = Counter(item.metric for item in series)
            if max(pods_per_metric.values(), default=0) > MAX_POD_SERIES:
                raise InvalidInputError(
                    "too many pods in range; narrow the time range or use groupBy=total",
                    max_pods=MAX_POD_SERIES,
                )
        return series

    async def _search_server_metrics(
        self,
        namespace: str,
        start: datetime,
        end: datetime,
        step: int,
        group_by: MetricGrouping,
    ) -> list[MetricSeries]:
        if self._metric_sample_repository is None:
            raise NotConfiguredError("on-prem metric storage is not configured")
        service_id = int(namespace.removeprefix("svc-"))
        samples = await self._metric_sample_repository.search_by_service_id(service_id, start, end)
        return bucket_server_metrics(samples, start, step, group_by)

    async def search_traffic_metrics(
        self, target_id: int, namespace: str, start: datetime, end: datetime, step: int
    ) -> TrafficMetrics:
        self._require_collected_target(target_id)
        if (end - start).total_seconds() / step > 1440:
            raise InvalidInputError("range and step may produce at most 1440 points per metric")
        return await self._client.search_traffic_metrics(
            self._get_url(target_id, "prometheus_url"),
            namespace,
            self._traffic_cluster,
            start.timestamp(),
            end.timestamp(),
            step,
        )

    async def _search_onprem_logs(
        self,
        target_id: int,
        namespace: str,
        start_ns: int,
        end_ns: int,
        limit: int,
        search: str,
        direction: Literal["forward", "backward"],
    ) -> list[LogEntry]:
        """Argo CD 가 읽어 온 지금 Pod 들의 로그. 지워진 Pod 의 로그(과거 이력)는 없다."""
        if self._pod_log_client is None:
            raise NotConfiguredError(
                "on-prem log backend is not configured",
                target_id=target_id,
                setting="ARGOCD_SERVER_URL, ARGOCD_LOGS_TOKEN",
            )
        since_seconds = math.ceil((time.time_ns() - start_ns) / 10**9) + 1
        # Application 이름은 namespace 와 같은 svc-{id} 다.
        entries = await self._pod_log_client.search_pod_logs(
            namespace, namespace, APP_CONTAINER, since_seconds, ONPREM_TAIL_LINES
        )
        matched = [
            entry
            for entry in entries
            if start_ns <= int(entry.timestamp_ns) < end_ns and search in entry.message
        ]
        return matched[:limit] if direction == "forward" else matched[-limit:]

    async def _search_stream_logs(
        self, target_id: int, namespace: str, start_ns: int, end_ns: int, search: str
    ) -> list[LogEntry]:
        if self._is_onprem(target_id):
            return await self._search_onprem_logs(
                target_id, namespace, start_ns, end_ns, 1000, search, "forward"
            )
        return await self._client.search_logs(
            self._get_url(target_id, "loki_url"),
            namespace,
            start_ns,
            end_ns,
            1000,
            search,
            "forward",
        )

    async def prepare_stream(
        self, target_id: int, namespace: str, start_ns: int, end_ns: int, search: str
    ) -> list[LogEntry]:
        return await self._search_stream_logs(target_id, namespace, start_ns, end_ns, search)

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
            # on-prem 은 매번 Argo CD 가 tailnet 너머 kubelet 을 읽으므로 덜 자주 묻는다.
            await asyncio.sleep(5 if self._is_onprem(target_id) else 2)
            try:
                entries = await self._search_stream_logs(
                    target_id,
                    namespace,
                    max(lower_bound, cursor - 10 * 10**9),
                    time.time_ns(),
                    search,
                )
            except ExternalError:
                yield 'event: error\ndata: {"code":"EXTERNAL_ERROR"}\n\n'
                return
