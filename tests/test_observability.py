import json
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

from app.clients.observability_client import (
    LogEntry,
    LokiPrometheusObservabilityClient,
    MetricPoint,
    MetricSeries,
    TrafficMetrics,
)
from app.core.config import Settings
from app.core.exceptions import ExternalError, InvalidInputError, ServiceNotFoundError
from app.dependencies import get_current_user, get_observability_service, get_session
from app.main import app
from app.models.user import User
from app.services.observability_service import MAX_POD_SERIES, ObservabilityService


def make_service(client=None, loki_url=None, owned=True, prometheus_url=None):
    repository = AsyncMock()
    repository.find_by_id_and_owner_id.return_value = object() if owned else None
    repository.search_target_ids_by_service_ids.return_value = {42: [1]}
    return ObservabilityService(repository, client or AsyncMock(), loki_url, prometheus_url)


async def test_loki_query_is_scoped_and_search_cannot_inject_logql():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "streams",
                    "result": [
                        {
                            "stream": {"k8s_pod_name": "app-b", "k8s_container_name": "app"},
                            "values": [["200", "second\nline"]],
                        },
                        {
                            "stream": {"k8s_pod_name": "app-a", "k8s_container_name": "app"},
                            "values": [["100", "first"]],
                        },
                    ],
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        search = '"} |~ ".*'
        entries = await LokiPrometheusObservabilityClient(http).search_logs(
            "http://loki", "svc-42", 1, 300, 100, search
        )
    assert [(entry.timestamp_ns, entry.pod, entry.container) for entry in entries] == [
        ("100", "app-a", "app"),
        ("200", "app-b", "app"),
    ]
    assert requests[0].url.params["query"] == (
        '{k8s_namespace_name="svc-42",k8s_container_name="app"} |= ' + json.dumps(search)
    )
    assert requests[0].url.params["direction"] == "backward"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"status": "error", "error": "private endpoint detail"}),
        httpx.Response(200, json={"status": "success", "data": {"resultType": "vector"}}),
    ],
)
async def test_loki_failures_are_domain_errors_without_upstream_details(response):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as http:
        with pytest.raises(ExternalError) as error:
            await LokiPrometheusObservabilityClient(http).search_logs(
                "http://loki", "svc-42", 1, 2, 100, ""
            )
    assert "private endpoint" not in str(error.value)


async def test_prometheus_queries_are_scoped_and_nonfinite_values_are_omitted():
    queries = []

    def handler(request):
        queries.append(request.url.params["query"])
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": [
                        {"metric": {}, "values": [[100, "0.25"], [160, "NaN"], [220, "+Inf"]]}
                    ],
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        series = await LokiPrometheusObservabilityClient(http).search_metrics(
            "http://prom", "svc-42", 1, 300, 60
        )
    assert len(series) == 4
    # OTel kubeletstats → Prometheus remote write 이름(2026-10-02 dev 클러스터에서 확인).
    assert queries == [
        'sum(rate(k8s_pod_cpu_time_seconds_total{k8s_namespace_name="svc-42"}[5m]))',
        'sum(k8s_pod_memory_working_set_bytes{k8s_namespace_name="svc-42"})',
        'sum(rate(k8s_pod_network_io_bytes_total{k8s_namespace_name="svc-42",direction="receive"}[5m]))',
        'sum(rate(k8s_pod_network_io_bytes_total{k8s_namespace_name="svc-42",direction="transmit"}[5m]))',
    ]
    assert [(point.timestamp, point.value) for point in series[0].points] == [(100, 0.25)]
    assert [item.unit for item in series] == ["cores", "bytes", "bytes/s", "bytes/s"]


async def test_scope_rejects_other_owner_before_querying_backends():
    service = make_service(owned=False)
    with pytest.raises(ServiceNotFoundError):
        await service.get_scope(9, 42, 1)
    service._service_repository.search_target_ids_by_service_ids.assert_not_awaited()
    service._client.search_logs.assert_not_awaited()


async def test_scope_rejects_unassigned_target():
    with pytest.raises(InvalidInputError):
        await make_service().get_scope(1, 42, 2)


@pytest.mark.parametrize(
    "start,end",
    [
        (datetime(2026, 1, 1), datetime(2026, 1, 2)),
        (datetime.now(UTC), datetime.now(UTC) - timedelta(hours=1)),
        (datetime.now(UTC) - timedelta(days=8), datetime.now(UTC)),
        (datetime.now(UTC), datetime.now(UTC) + timedelta(hours=1)),
    ],
)
def test_range_rejects_naive_reversed_excessive_or_future_times(start, end):
    with pytest.raises(InvalidInputError):
        ObservabilityService.validate_range(start, end)


async def test_metrics_rejects_excessive_samples_before_query():
    service = make_service()
    end = datetime.now(UTC)
    with pytest.raises(InvalidInputError):
        await service.search_metrics(1, "svc-42", end - timedelta(days=7), end, 15)
    service._client.search_metrics.assert_not_awaited()


async def test_stream_emits_safe_json_and_resume_cursor_then_closes():
    timestamp = time.time_ns()
    entry = LogEntry(str(timestamp), "message\nevent: injected", "app-a", "app")
    stream = make_service().stream_logs(1, "svc-42", timestamp, "", [entry])
    event = await anext(stream)
    assert event.startswith(f"id: {timestamp + 1}\nevent: logs\ndata: ")
    assert len(event.splitlines()) == 4
    assert json.loads(event.split("data: ")[1])[0]["message"] == entry.message
    await stream.aclose()


async def test_stream_deduplicates_overlap_and_keeps_late_entries(monkeypatch):
    timestamp = time.time_ns()
    first = LogEntry(str(timestamp), "first", "app-a", "app")
    late = LogEntry(str(timestamp - 1), "late", "app-a", "app")
    client = AsyncMock()
    client.search_logs.return_value = [first, late]
    service = make_service(client, "http://loki")
    monkeypatch.setattr("app.services.observability_service.asyncio.sleep", AsyncMock())
    stream = service.stream_logs(1, "svc-42", timestamp - 10**9, "", [first])
    await anext(stream)
    event = await anext(stream)
    assert json.loads(event.split("data: ")[1]) == [
        {"timestampNs": late.timestamp_ns, "message": "late", "pod": "app-a", "container": "app"}
    ]
    assert event.startswith(f"id: {timestamp + 1}")
    await stream.aclose()


async def test_stream_overflow_is_explicit_without_advancing_cursor():
    stream = make_service().stream_logs(1, "svc-42", 100, "", [LogEntry("100", "x", "", "")] * 1000)
    assert "event: overflow" in await anext(stream)
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


async def test_stream_heartbeat_and_upstream_error(monkeypatch):
    client = AsyncMock()
    client.search_logs.side_effect = ExternalError()
    service = make_service(client, "http://loki")
    monkeypatch.setattr("app.services.observability_service.asyncio.sleep", AsyncMock())
    stream = service.stream_logs(1, "svc-42", time.time_ns(), "", [])
    assert await anext(stream) == ": heartbeat\n\n"
    assert "event: error" in await anext(stream)
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


@pytest.fixture
async def api_client():
    user = User(github_id=1, login="owner")
    user.id = 1
    session = AsyncMock()
    service = make_service()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_observability_service] = lambda: service
    app.dependency_overrides[get_session] = lambda: session
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://api"
        ) as http:
            yield http, service, session
    finally:
        app.dependency_overrides.clear()


async def test_logs_api_returns_camelcase_and_releases_database_session(api_client):
    http, service, session = api_client
    service.search_logs = AsyncMock(return_value=[LogEntry("100", "ready", "app-a", "app")])
    response = await http.get(
        "/api/v1/services/42/logs",
        params={"targetId": 1, "start": "2026-01-01T00:00:00Z", "end": "2026-01-01T01:00:00Z"},
    )
    assert response.status_code == 200
    assert response.json()["data"] == {
        "entries": [{"timestampNs": "100", "message": "ready", "pod": "app-a", "container": "app"}],
        "isTruncated": False,
    }
    session.close.assert_awaited_once()


async def test_unconfigured_backend_returns_503_before_sse_headers(api_client):
    http, _, session = api_client
    response = await http.get("/api/v1/services/42/logs/stream", params={"targetId": 1})
    assert response.status_code == 503
    assert response.json()["code"] == "NOT_CONFIGURED"
    assert response.headers["content-type"] == "application/json"
    session.close.assert_awaited_once()


@pytest.mark.parametrize("cursor", ["bad", "-1", str(2**63), "9" * 21])
async def test_stream_invalid_cursor_returns_422(api_client, cursor):
    http, _, _ = api_client
    response = await http.get(
        "/api/v1/services/42/logs/stream", params={"targetId": 1, "cursor": cursor}
    )
    assert response.status_code == 422


async def test_sse_content_type_headers_and_last_event_id(api_client, monkeypatch):
    http, service, session = api_client
    timestamp = time.time_ns() - 10**9
    service.prepare_stream = AsyncMock(return_value=[LogEntry(str(timestamp), "ready", "", "app")])
    # 첫 이벤트 후 종료하여 ASGI 테스트가 무한 스트림을 버퍼링하지 않는다.
    original_stream = service.stream_logs

    async def finite_stream(*args):
        generator = original_stream(*args)
        try:
            yield await anext(generator)
        finally:
            await generator.aclose()

    service.stream_logs = finite_stream
    response = await http.get(
        "/api/v1/services/42/logs/stream",
        params={"targetId": 1, "cursor": "bad"},
        headers={"Last-Event-ID": str(timestamp)},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    assert f"id: {timestamp + 1}" in response.text
    assert service.prepare_stream.call_args.args[2] == timestamp
    session.close.assert_awaited_once()


async def test_empty_prometheus_result_remains_empty():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"status": "success", "data": {"resultType": "matrix", "result": []}}
            )
        )
    ) as http:
        series = await LokiPrometheusObservabilityClient(http).search_metrics(
            "http://prom", "svc-42", 1, 300, 60
        )
    assert all(item.points == [] for item in series)


async def test_metrics_api_serializes_real_samples(api_client):
    http, service, session = api_client
    service.search_metrics = AsyncMock(
        return_value=[MetricSeries("cpu", "cores", [MetricPoint(1767225600, 0.25)])]
    )
    response = await http.get(
        "/api/v1/services/42/metrics",
        params={"targetId": 1, "start": "2026-01-01T00:00:00Z", "end": "2026-01-01T01:00:00Z"},
    )
    assert response.status_code == 200
    assert response.json()["data"] == [
        {"metric": "cpu", "unit": "cores", "points": [{"timestamp": 1767225600, "value": 0.25}]}
    ]
    session.close.assert_awaited_once()


async def test_api_unauthenticated_request_returns_401(api_client):
    from app.core.exceptions import UnauthorizedError

    http, _, _ = api_client

    async def no_user():
        raise UnauthorizedError("login required")

    app.dependency_overrides[get_current_user] = no_user
    response = await http.get("/api/v1/services/42/logs/stream", params={"targetId": 1})
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


async def test_api_other_owner_returns_404_before_backend_request(api_client):
    http, service, _ = api_client
    service._service_repository.find_by_id_and_owner_id.return_value = None
    response = await http.get("/api/v1/services/42/logs/stream", params={"targetId": 1})
    assert response.status_code == 404
    service._client.search_logs.assert_not_awaited()


def test_backend_urls_are_read_from_env(monkeypatch):
    monkeypatch.setenv("LOKI_URL", "http://loki.observability:3100")
    monkeypatch.setenv("PROMETHEUS_URL", "http://monitoring-prometheus.observability:9090")
    settings = Settings(_env_file=None, database_url="postgresql+asyncpg://x")
    assert str(settings.loki_url) == "http://loki.observability:3100/"
    assert str(settings.prometheus_url) == "http://monitoring-prometheus.observability:9090/"


def _matrix_response(result):
    return httpx.Response(
        200, json={"status": "success", "data": {"resultType": "matrix", "result": result}}
    )


async def test_prometheus_pod_grouping_returns_one_series_per_pod_sorted_by_name():
    queries = []

    def handler(request):
        queries.append(request.url.params["query"])
        return _matrix_response(
            [
                {"metric": {"k8s_pod_name": "app-b"}, "values": [[100, "2"], [160, "NaN"]]},
                {"metric": {"k8s_pod_name": "app-a"}, "values": [[100, "1"], [160, "3"]]},
            ]
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        series = await LokiPrometheusObservabilityClient(http).search_metrics(
            "http://prom", "svc-42", 1, 300, 60, "pod"
        )
    cpu_time = "k8s_pod_cpu_time_seconds_total"
    network_io = "k8s_pod_network_io_bytes_total"
    selector = 'k8s_namespace_name="svc-42"'
    assert queries == [
        f"sum by (k8s_pod_name) (rate({cpu_time}{{{selector}}}[5m]))",
        f"sum by (k8s_pod_name) (k8s_pod_memory_working_set_bytes{{{selector}}})",
        f'sum by (k8s_pod_name) (rate({network_io}{{{selector},direction="receive"}}[5m]))',
        f'sum by (k8s_pod_name) (rate({network_io}{{{selector},direction="transmit"}}[5m]))',
    ]
    assert [(item.metric, item.pod) for item in series] == [
        (metric, pod)
        for metric in ("cpu", "memory", "network_receive", "network_transmit")
        for pod in ("app-a", "app-b")
    ]
    assert [(point.timestamp, point.value) for point in series[0].points] == [(100, 1), (160, 3)]
    assert [(point.timestamp, point.value) for point in series[1].points] == [(100, 2)]


async def test_prometheus_pod_grouping_without_data_returns_no_series():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: _matrix_response([]))
    ) as http:
        series = await LokiPrometheusObservabilityClient(http).search_metrics(
            "http://prom", "svc-42", 1, 300, 60, "pod"
        )
    assert series == []


@pytest.mark.parametrize("labels", [{}, {"k8s_pod_name": ""}, {"k8s_pod_name": 1}])
async def test_prometheus_pod_grouping_rejects_series_without_pod_label(labels):
    response = _matrix_response([{"metric": labels, "values": [[100, "1"]]}])
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as http:
        with pytest.raises(ExternalError):
            await LokiPrometheusObservabilityClient(http).search_metrics(
                "http://prom", "svc-42", 1, 300, 60, "pod"
            )


async def test_prometheus_total_rejects_multiple_series():
    response = _matrix_response(
        [
            {"metric": {"k8s_pod_name": "app-a"}, "values": [[100, "1"]]},
            {"metric": {"k8s_pod_name": "app-b"}, "values": [[100, "1"]]},
        ]
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as http:
        with pytest.raises(ExternalError):
            await LokiPrometheusObservabilityClient(http).search_metrics(
                "http://prom", "svc-42", 1, 300, 60
            )


async def test_metrics_rejects_too_many_pod_series():
    client = AsyncMock()
    client.search_metrics.return_value = [
        MetricSeries("cpu", "cores", [], f"app-{index}") for index in range(MAX_POD_SERIES + 1)
    ]
    service = make_service(client, prometheus_url="http://prom")
    end = datetime.now(UTC)
    with pytest.raises(InvalidInputError):
        await service.search_metrics(1, "svc-42", end - timedelta(hours=1), end, 60, "pod")


async def test_metrics_accepts_pod_series_up_to_limit_and_forwards_grouping():
    series = [MetricSeries("cpu", "cores", [], f"app-{index}") for index in range(MAX_POD_SERIES)]
    client = AsyncMock()
    client.search_metrics.return_value = series
    service = make_service(client, prometheus_url="http://prom")
    end = datetime.now(UTC)
    result = await service.search_metrics(1, "svc-42", end - timedelta(hours=1), end, 60, "pod")
    assert result == series
    assert client.search_metrics.call_args.args[-1] == "pod"


async def test_metrics_api_group_by_pod_serializes_pod_and_omits_it_for_total(api_client):
    http, service, _ = api_client
    params = {"targetId": 1, "start": "2026-01-01T00:00:00Z", "end": "2026-01-01T01:00:00Z"}
    service.search_metrics = AsyncMock(
        return_value=[MetricSeries("cpu", "cores", [MetricPoint(1767225600, 0.25)], "app-a")]
    )
    response = await http.get("/api/v1/services/42/metrics", params={**params, "groupBy": "pod"})
    assert response.status_code == 200
    assert response.json()["data"] == [
        {
            "metric": "cpu",
            "unit": "cores",
            "points": [{"timestamp": 1767225600, "value": 0.25}],
            "pod": "app-a",
        }
    ]
    assert service.search_metrics.call_args.args[-1] == "pod"

    service.search_metrics = AsyncMock(return_value=[MetricSeries("cpu", "cores", [])])
    response = await http.get("/api/v1/services/42/metrics", params=params)
    assert response.json()["data"] == [{"metric": "cpu", "unit": "cores", "points": []}]
    assert service.search_metrics.call_args.args[-1] == "total"


async def test_metrics_api_rejects_unknown_group_by(api_client):
    http, _, _ = api_client
    response = await http.get(
        "/api/v1/services/42/metrics",
        params={
            "targetId": 1,
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-01-01T01:00:00Z",
            "groupBy": "namespace",
        },
    )
    assert response.status_code == 422


TRAFFIC_EVENT_START = 1767225600  # 2026-01-01T00:00:00Z
TRAFFIC_SELECTOR = 'cluster="iris-dev-workload",k8s_namespace_name="svc-42"'


def _traffic_handler(requests, values_by_prefix=None):
    values_by_prefix = values_by_prefix or {}

    def handler(request):
        query = request.url.params["query"]
        requests.append(request)
        for prefix, result in values_by_prefix.items():
            if query.startswith(prefix):
                return _matrix_response(result)
        return _matrix_response([])

    return handler


async def test_traffic_queries_use_step_windows_and_shift_points_to_event_time():
    requests = []
    sample_start = TRAFFIC_EVENT_START + 60 + 900
    handler = _traffic_handler(
        requests,
        {
            "sum_over_time(iris_service_requests_1m": [
                {"metric": {}, "values": [[sample_start, "12"], [sample_start + 60, "NaN"]]}
            ],
            "last_over_time(iris_service_response_time_seconds_p95_5m": [
                {"metric": {}, "values": [[sample_start + 120, "0.25"]]}
            ],
        },
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        traffic = await LokiPrometheusObservabilityClient(http).search_traffic_metrics(
            "http://prom",
            "svc-42",
            "iris-dev-workload",
            TRAFFIC_EVENT_START,
            TRAFFIC_EVENT_START + 3600,
            60,
        )
    selector = TRAFFIC_SELECTOR
    errors = 'iris_service_errors_1m{%s,status_class="%s"}'
    requests_sum = f"sum_over_time(iris_service_requests_1m{{{selector}}}[60s])"
    bytes_name = "iris_service_public_network_bytes_1m"
    assert [request.url.params["query"] for request in requests] == [
        requests_sum,
        f"sum_over_time({errors % (selector, '4xx')}[60s]) / ignoring(status_class) {requests_sum}",
        f"sum_over_time({errors % (selector, '5xx')}[60s]) / ignoring(status_class) {requests_sum}",
        f'sum_over_time({bytes_name}{{{selector},direction="receive"}}[60s]) / 60',
        f'sum_over_time({bytes_name}{{{selector},direction="transmit"}}[60s]) / 60',
        f"last_over_time(iris_service_response_time_seconds_avg5m{{{selector}}}[60s])",
        f"last_over_time(iris_service_response_time_seconds_p50_5m{{{selector}}}[60s])",
        f"last_over_time(iris_service_response_time_seconds_p95_5m{{{selector}}}[60s])",
    ]
    params = requests[0].url.params
    assert (float(params["start"]), float(params["end"]), params["step"]) == (
        sample_start,
        TRAFFIC_EVENT_START + 3600 + 900,
        "60",
    )
    assert [(item.metric, item.unit) for item in traffic.series] == [
        ("requests", "requests"),
        ("error_rate_4xx", "ratio"),
        ("error_rate_5xx", "ratio"),
        ("public_network_receive", "bytes/s"),
        ("public_network_transmit", "bytes/s"),
        ("response_time_avg", "seconds"),
        ("response_time_p50", "seconds"),
        ("response_time_p95", "seconds"),
    ]
    # 샘플 시각은 이벤트 시각보다 15분 늦다. 결과는 버킷이 끝나는 이벤트 시각이다.
    assert traffic.series[0].points == [MetricPoint(TRAFFIC_EVENT_START + 60, 12)]
    assert traffic.series[7].points == [MetricPoint(TRAFFIC_EVENT_START + 180, 0.25)]
    # 샘플이 없는 지표는 결측이라 0 으로 채우지 않는다.
    assert traffic.series[1].points == []
    assert traffic.available_until == pytest.approx(time.time() - 900, abs=5)


async def test_traffic_range_inside_the_pending_window_returns_empty_series_without_query():
    def fail(request):
        raise AssertionError("no query expected")

    now = time.time()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as http:
        traffic = await LokiPrometheusObservabilityClient(http).search_traffic_metrics(
            "http://prom", "svc-42", "iris-dev-workload", now - 600, now - 60, 60
        )
    assert len(traffic.series) == 8
    assert all(item.points == [] for item in traffic.series)


async def test_traffic_query_end_is_clamped_to_now(monkeypatch):
    requests = []
    monkeypatch.setattr("app.clients.observability_client.time.time", lambda: 1_000_000.0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(_traffic_handler(requests))) as http:
        await LokiPrometheusObservabilityClient(http).search_traffic_metrics(
            "http://prom", "svc-42", "iris-dev-workload", 990_000.0, 999_500.0, 60
        )
    assert float(requests[0].url.params["end"]) == 1_000_000.0


async def test_traffic_rejects_more_than_one_series():
    two_series = [
        {"metric": {"pod": "a"}, "values": [[TRAFFIC_EVENT_START + 960, "1"]]},
        {"metric": {"pod": "b"}, "values": [[TRAFFIC_EVENT_START + 960, "1"]]},
    ]
    handler = _traffic_handler([], {"sum_over_time(iris_service_requests_1m": two_series})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ExternalError):
            await LokiPrometheusObservabilityClient(http).search_traffic_metrics(
                "http://prom",
                "svc-42",
                "iris-dev-workload",
                TRAFFIC_EVENT_START,
                TRAFFIC_EVENT_START + 3600,
                60,
            )


async def test_traffic_backend_failure_is_a_domain_error():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(503))
    ) as http:
        with pytest.raises(ExternalError):
            await LokiPrometheusObservabilityClient(http).search_traffic_metrics(
                "http://prom",
                "svc-42",
                "iris-dev-workload",
                TRAFFIC_EVENT_START,
                TRAFFIC_EVENT_START + 3600,
                60,
            )


async def test_traffic_service_rejects_excessive_samples_before_query():
    service = make_service(prometheus_url="http://prom")
    end = datetime.now(UTC)
    with pytest.raises(InvalidInputError):
        await service.search_traffic_metrics(1, "svc-42", end - timedelta(days=7), end, 60)
    service._client.search_traffic_metrics.assert_not_awaited()


async def test_traffic_service_passes_configured_cluster_and_range():
    client = AsyncMock()
    expected = TrafficMetrics(1.0, [])
    client.search_traffic_metrics.return_value = expected
    repository = AsyncMock()
    service = ObservabilityService(
        repository, client, None, "http://prom", traffic_cluster="iris-prod-workload"
    )
    end = datetime.now(UTC)
    start = end - timedelta(hours=1)
    assert await service.search_traffic_metrics(1, "svc-42", start, end, 60) is expected
    assert client.search_traffic_metrics.await_args.args == (
        "http://prom",
        "svc-42",
        "iris-prod-workload",
        start.timestamp(),
        end.timestamp(),
        60,
    )


async def test_traffic_service_is_not_configured_without_prometheus():
    from app.core.exceptions import NotConfiguredError

    end = datetime.now(UTC)
    with pytest.raises(NotConfiguredError):
        await make_service().search_traffic_metrics(1, "svc-42", end - timedelta(hours=1), end, 60)


async def test_traffic_api_serializes_camelcase_and_omits_pod(api_client):
    http, service, session = api_client
    service.search_traffic_metrics = AsyncMock(
        return_value=TrafficMetrics(
            1767225600.0,
            [MetricSeries("requests", "requests", [MetricPoint(1767225540, 3)])],
        )
    )
    response = await http.get(
        "/api/v1/services/42/traffic-metrics",
        params={"targetId": 1, "start": "2026-01-01T00:00:00Z", "end": "2026-01-01T01:00:00Z"},
    )
    assert response.status_code == 200
    assert response.json()["data"] == {
        "availableUntil": 1767225600.0,
        "series": [
            {
                "metric": "requests",
                "unit": "requests",
                "points": [{"timestamp": 1767225540, "value": 3}],
            }
        ],
    }
    assert service.search_traffic_metrics.call_args.args[-1] == 60
    session.close.assert_awaited_once()


@pytest.mark.parametrize("step", [30, 59, 0, 86401])
async def test_traffic_api_rejects_step_outside_range(api_client, step):
    http, _, _ = api_client
    response = await http.get(
        "/api/v1/services/42/traffic-metrics",
        params={
            "targetId": 1,
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-01-01T01:00:00Z",
            "step": step,
        },
    )
    assert response.status_code == 422


async def test_traffic_api_other_owner_returns_404_before_backend_request(api_client):
    http, service, _ = api_client
    service._service_repository.find_by_id_and_owner_id.return_value = None
    response = await http.get(
        "/api/v1/services/42/traffic-metrics",
        params={"targetId": 1, "start": "2026-01-01T00:00:00Z", "end": "2026-01-01T01:00:00Z"},
    )
    assert response.status_code == 404
    service._client.search_traffic_metrics.assert_not_awaited()


async def test_traffic_api_requires_login(api_client):
    from app.core.exceptions import UnauthorizedError

    http, _, _ = api_client

    async def no_user():
        raise UnauthorizedError("login required")

    app.dependency_overrides[get_current_user] = no_user
    response = await http.get(
        "/api/v1/services/42/traffic-metrics",
        params={"targetId": 1, "start": "2026-01-01T00:00:00Z", "end": "2026-01-01T01:00:00Z"},
    )
    assert response.status_code == 401
