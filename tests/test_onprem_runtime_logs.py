import json
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr

from app.clients.argocd_client import ArgoCdClient
from app.clients.observability_client import LogEntry
from app.core.config import Settings
from app.core.exceptions import ExternalError, NotConfiguredError
from app.dependencies import build_observability_service
from app.enums import TargetKind
from app.services.observability_service import (
    APP_CONTAINER,
    ONPREM_TAIL_LINES,
    ObservabilityService,
)

ARGO_URL = "https://argocd.internal"


def _argo_body(*items: dict[str, object]) -> str:
    return "\n".join(json.dumps(item) for item in items) + "\n"


def _result(content: str, stamp: str, pod: str = "svc-42-abc", last: bool = False) -> dict:
    return {
        "result": {
            "content": content,
            "timeStamp": stamp[:19] + "Z",
            "timeStampStr": stamp,
            "podName": pod,
            "last": last,
        }
    }


def _argo_client(handler) -> ArgoCdClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ArgoCdClient(http, base_url=ARGO_URL + "/", token=SecretStr("log-token"))


async def test_search_pod_logs_requests_app_logs_and_parses_nanosecond_entries():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            text=_argo_body(
                _result("second", "2026-10-04T01:00:01.5Z", pod="svc-42-b"),
                _result("first", "2026-10-04T01:00:00.000000123Z"),
                _result("", "2026-10-04T01:00:02Z", last=True),
            ),
        )

    entries = await _argo_client(handler).search_pod_logs("svc-42", "svc-42", "app", 60, 500)

    base = int(datetime(2026, 10, 4, 1, tzinfo=UTC).timestamp()) * 10**9
    assert entries == [
        LogEntry(str(base + 123), "first", "svc-42-abc", "app"),
        LogEntry(str(base + 1_500_000_000), "second", "svc-42-b", "app"),
    ]
    request = requests[0]
    assert str(request.url).startswith(f"{ARGO_URL}/api/v1/applications/svc-42/logs?")
    assert dict(request.url.params) == {
        "namespace": "svc-42",
        "container": "app",
        "sinceSeconds": "60",
        "tailLines": "500",
        "follow": "false",
    }
    assert request.headers["Authorization"] == "Bearer log-token"


@pytest.mark.parametrize("status_code", [403, 404])
async def test_search_pod_logs_missing_application_returns_empty(status_code):
    client = _argo_client(lambda request: httpx.Response(status_code))
    assert await client.search_pod_logs("svc-42", "svc-42", "app", 60, 500) == []


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="boom"),
        httpx.Response(200, text=_argo_body({"error": {"message": "max pods reached"}})),
        httpx.Response(200, text="not json"),
        httpx.Response(200, text=_argo_body(_result("x", "yesterday"))),
    ],
)
async def test_search_pod_logs_failures_are_external_errors(response):
    client = _argo_client(lambda request: response)
    with pytest.raises(ExternalError):
        await client.search_pod_logs("svc-42", "svc-42", "app", 60, 500)


async def test_search_pod_logs_network_error_is_external_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(ExternalError):
        await _argo_client(handler).search_pod_logs("svc-42", "svc-42", "app", 60, 500)


def _service(kind: TargetKind, pod_log_client=None, loki_url="http://loki"):
    services = AsyncMock()
    services.find_by_id_and_owner_id.return_value = object()
    services.search_target_ids_by_service_ids.return_value = {42: [7]}
    targets = AsyncMock()
    targets.search_by_ids.return_value = [SimpleNamespace(kind=kind)]
    return ObservabilityService(
        services,
        AsyncMock(),
        loki_url,
        "http://prom",
        target_repository=targets,
        pod_log_client=pod_log_client,
    )


def _entry(offset_s: int, message: str) -> LogEntry:
    return LogEntry(str(time.time_ns() - offset_s * 10**9), message, "svc-42-a", "app")


async def test_onprem_logs_read_argo_with_time_window_search_and_limit():
    old, a, b, c = _entry(600, "ok old"), _entry(50, "ok a"), _entry(40, "skip"), _entry(30, "ok c")
    argo = AsyncMock()
    argo.search_pod_logs.return_value = [old, a, b, c]
    service = _service(TargetKind.ONPREM, argo)
    namespace = await service.get_scope(1, 42, 7)
    start_ns = time.time_ns() - 120 * 10**9

    entries = await service.search_logs(7, namespace, start_ns, time.time_ns(), 1, "ok")

    assert entries == [c]
    service._client.search_logs.assert_not_awaited()
    application, ns, container, since_seconds, tail_lines = argo.search_pod_logs.await_args.args
    assert (application, ns, container, tail_lines) == (
        "svc-42",
        "svc-42",
        APP_CONTAINER,
        ONPREM_TAIL_LINES,
    )
    assert 120 <= since_seconds <= 125


async def test_onprem_stream_prepare_reads_argo_forward():
    a, b = _entry(5, "a"), _entry(3, "b")
    argo = AsyncMock()
    argo.search_pod_logs.return_value = [a, b]
    service = _service(TargetKind.ONPREM, argo)
    await service.get_scope(1, 42, 7)

    initial = await service.prepare_stream(7, "svc-42", time.time_ns() - 10**10, time.time_ns(), "")

    assert initial == [a, b]
    service._client.search_logs.assert_not_awaited()


async def test_aws_logs_still_read_loki():
    argo = AsyncMock()
    service = _service(TargetKind.AWS, argo)
    service._client.search_logs.return_value = []
    await service.get_scope(1, 42, 7)

    await service.search_logs(7, "svc-42", 1, 2, 10, "")

    service._client.search_logs.assert_awaited_once()
    argo.search_pod_logs.assert_not_awaited()


async def test_onprem_logs_without_argo_token_are_not_configured():
    service = _service(TargetKind.ONPREM, None)
    await service.get_scope(1, 42, 7)
    with pytest.raises(NotConfiguredError):
        await service.search_logs(7, "svc-42", 1, time.time_ns(), 10, "")


async def test_onprem_argo_errors_propagate_as_external_errors():
    argo = AsyncMock()
    argo.search_pod_logs.side_effect = ExternalError("argocd log request failed")
    service = _service(TargetKind.ONPREM, argo)
    await service.get_scope(1, 42, 7)
    with pytest.raises(ExternalError):
        await service.search_logs(7, "svc-42", 1, time.time_ns(), 10, "")


async def test_onprem_metrics_and_network_logs_are_not_available():
    service = _service(TargetKind.ONPREM, AsyncMock())
    await service.get_scope(1, 42, 7)
    end = datetime.now(UTC)
    start = end - timedelta(hours=1)
    with pytest.raises(NotConfiguredError):
        await service.search_metrics(7, "svc-42", start, end, 60)
    with pytest.raises(NotConfiguredError):
        await service.search_traffic_metrics(7, "svc-42", start, end, 60)
    with pytest.raises(NotConfiguredError):
        await service.search_network_logs(7, "svc-42", 1, 2, 10, None)
    service._client.search_metrics.assert_not_awaited()
    service._client.search_network_logs.assert_not_awaited()


def _settings(**values: object) -> Settings:
    return Settings(database_url="postgresql+asyncpg://unused/db", **values)  # type: ignore[arg-type]


def test_argo_log_client_is_built_only_with_url_and_token():
    session = AsyncMock()
    http = httpx.AsyncClient()
    configured = build_observability_service(
        session,
        _settings(argocd_server_url=ARGO_URL, argocd_logs_token="t"),
        http,
    )
    missing_token = build_observability_service(
        session, _settings(argocd_server_url=ARGO_URL), http
    )
    assert isinstance(configured._pod_log_client, ArgoCdClient)
    assert missing_token._pod_log_client is None
