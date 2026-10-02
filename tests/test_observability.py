import json
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

from app.clients.observability_client import LogEntry, LokiPrometheusObservabilityClient
from app.core.config import ObservabilityEndpoint, Settings
from app.core.exceptions import ExternalError, InvalidInputError, ServiceNotFoundError
from app.dependencies import get_current_user, get_observability_service, get_session
from app.main import app
from app.models.user import User
from app.services.observability_service import ObservabilityService


def make_service(client=None, endpoints=None, owned=True):
    repository = AsyncMock()
    repository.find_by_id_and_owner_id.return_value = object() if owned else None
    repository.search_target_ids_by_service_ids.return_value = {42: [1]}
    return ObservabilityService(repository, client or AsyncMock(), endpoints or {})


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
    service = make_service(client, {1: ObservabilityEndpoint(loki_url="http://loki")})
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
    service = make_service(client, {1: ObservabilityEndpoint(loki_url="http://loki")})
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
    from app.clients.observability_client import MetricPoint, MetricSeries

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


def test_observability_endpoints_are_read_from_separate_env_vars(monkeypatch):
    monkeypatch.setenv("OBSERVABILITY_ENDPOINTS__1__LOKI_URL", "http://loki.observability:3100")
    monkeypatch.setenv(
        "OBSERVABILITY_ENDPOINTS__1__PROMETHEUS_URL",
        "http://monitoring-prometheus.observability:9090",
    )
    settings = Settings(_env_file=None, database_url="postgresql+asyncpg://x")
    endpoint = settings.observability_endpoints[1]
    assert str(endpoint.loki_url) == "http://loki.observability:3100/"
    assert str(endpoint.prometheus_url) == "http://monitoring-prometheus.observability:9090/"
