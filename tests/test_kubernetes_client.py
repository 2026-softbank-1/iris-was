import asyncio
import base64
import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.http11 import Request, Response

from app.clients.kubernetes_client import (
    CHANNEL_RESIZE,
    CHANNEL_STATUS,
    CHANNEL_STDIN,
    CHANNEL_STDOUT,
    ExecExit,
    ExecOutput,
    HttpKubernetesClient,
    _parse_status,
    build_cluster_ssl_context,
)
from app.core.exceptions import (
    ClusterUnavailableError,
    NotConfiguredError,
    PodNotFoundError,
    PodNotReadyError,
    ShellNotFoundError,
)

NAMESPACE = "svc-42"
TOKEN = "cluster-token"


class _Token:
    async def get_token(self) -> str:
        return TOKEN


def _pod(
    name: str,
    *,
    containers: tuple[str, ...] = ("app",),
    phase: str = "Running",
    is_ready: bool = True,
    start: str | None = "2026-10-04T11:00:00Z",
    labels: dict[str, str] | None = None,
    terminating: bool = False,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {"name": name, "labels": labels or {}}
    if terminating:
        metadata["deletionTimestamp"] = "2026-10-04T11:05:00Z"
    return {
        "metadata": metadata,
        "spec": {"containers": [{"name": c} for c in containers]},
        "status": {
            "phase": phase,
            "startTime": start,
            "containerStatuses": [{"name": c, "ready": is_ready} for c in containers],
        },
    }


def _rest_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> HttpKubernetesClient:
    http = httpx.AsyncClient(base_url="https://k8s.test", transport=httpx.MockTransport(handler))
    return HttpKubernetesClient(http, "https://k8s.test", None, _Token())


# ---- Pod 조회 -----------------------------------------------------------------------------


async def test_search_pods_returns_app_pods_newest_first() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "items": [
                    _pod("old", start="2026-10-04T10:00:00Z", labels={"iris/release-id": "7"}),
                    _pod("new", start="2026-10-04T11:00:00Z", labels={"iris/release-id": "8"}),
                    _pod("sidecar-only", containers=("istio",)),
                    _pod("pending", phase="Pending", is_ready=False, start=None),
                ]
            },
        )

    pods = await _rest_client(handler).search_pods(NAMESPACE, "app")

    assert [pod.name for pod in pods] == ["new", "old", "pending"]
    assert [pod.release_id for pod in pods] == [8, 7, None]
    assert pods[0].started_at == datetime(2026, 10, 4, 11, 0, tzinfo=UTC)
    assert pods[0].is_ready is True
    assert pods[2].is_ready is False
    assert str(seen[0].url) == f"https://k8s.test/api/v1/namespaces/{NAMESPACE}/pods"
    assert seen[0].headers["Authorization"] == f"Bearer {TOKEN}"


async def test_search_pods_marks_terminating_pod_not_ready() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [_pod("going", terminating=True)]})

    (pod,) = await _rest_client(handler).search_pods(NAMESPACE, "app")

    assert pod.phase == "Terminating"
    assert pod.is_ready is False


async def test_search_pods_container_not_ready_is_not_ready() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [_pod("booting", is_ready=False)]})

    (pod,) = await _rest_client(handler).search_pods(NAMESPACE, "app")

    assert pod.phase == "Running"
    assert pod.is_ready is False


@pytest.mark.parametrize("status_code", [401, 403, 500])
async def test_search_pods_cluster_error_raises_cluster_unavailable(status_code: int) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"message": "secret detail"})

    with pytest.raises(ClusterUnavailableError) as exc_info:
        await _rest_client(handler).search_pods(NAMESPACE, "app")

    assert "secret detail" not in exc_info.value.message


async def test_search_pods_network_error_raises_cluster_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(ClusterUnavailableError):
        await _rest_client(handler).search_pods(NAMESPACE, "app")


async def test_find_pod_missing_returns_none() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"kind": "Status"})

    assert await _rest_client(handler).find_pod(NAMESPACE, "gone", "app") is None


async def test_find_pod_without_app_container_returns_none() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_pod("other", containers=("istio",)))

    assert await _rest_client(handler).find_pod(NAMESPACE, "other", "app") is None


async def test_find_pod_escapes_path_segments() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(404)

    await _rest_client(handler).find_pod(NAMESPACE, "../secrets", "app")

    assert seen[0].url.raw_path.decode() == f"/api/v1/namespaces/{NAMESPACE}/pods/..%2Fsecrets"


# ---- 상태 프레임 해석 ------------------------------------------------------------------------


def test_parse_status_success_is_exit_code_zero() -> None:
    assert _parse_status(b'{"status":"Success"}') == ExecExit(exit_code=0)


def test_parse_status_non_zero_exit_code() -> None:
    payload = json.dumps(
        {
            "status": "Failure",
            "message": "command terminated with non-zero exit code",
            "reason": "NonZeroExitCode",
            "details": {"causes": [{"reason": "ExitCode", "message": "130"}]},
        }
    ).encode()

    assert _parse_status(payload).exit_code == 130


@pytest.mark.parametrize("payload", [b"not json", b"[]", b'{"status":"Failure"}'])
def test_parse_status_unknown_failure_has_no_exit_code(payload: bytes) -> None:
    assert _parse_status(payload).exit_code is None


# ---- exec (가짜 쿠버네티스 WebSocket 서버) ------------------------------------------------------


class FakeApiServer:
    """`v4.channel.k8s.io` 로 말하는 가짜 API 서버. 요청을 기록하고 시나리오대로 답한다."""

    def __init__(self) -> None:
        self.requests: list[Request] = []
        self.received: list[tuple[int, bytes]] = []
        self.probe_reply: list[bytes] = [
            bytes([CHANNEL_STDOUT]) + b"bash\n",
            bytes([CHANNEL_STATUS]) + b'{"status":"Success"}',
        ]
        self.reject_with: HTTPStatus | None = None
        self.reject_body = b""
        self.subprotocols: list[str] = ["v4.channel.k8s.io"]
        self.drop_after_greeting = False

    async def process_request(
        self, connection: ServerConnection, request: Request
    ) -> Response | None:
        self.requests.append(request)
        if self.reject_with is not None:
            response = connection.respond(self.reject_with, self.reject_body.decode())
            return response
        return None

    async def handle(self, ws: ServerConnection) -> None:
        query = parse_qs(urlsplit(ws.request.path).query)
        if query["tty"] == ["false"]:
            for frame in self.probe_reply:
                await ws.send(frame)
            await ws.close()
            return
        await ws.send(bytes([CHANNEL_STDOUT]))  # 빈 프레임은 건너뛴다
        await ws.send(bytes([CHANNEL_STDOUT]) + "안녕".encode()[:2])  # 문자가 잘린 채로
        await ws.send(bytes([CHANNEL_STDOUT]) + "안녕".encode()[2:])
        if self.drop_after_greeting:
            await ws.close()
            return
        async for message in ws:
            assert isinstance(message, bytes)
            channel, payload = message[0], message[1:]
            self.received.append((channel, payload))
            if channel == CHANNEL_STDIN:
                await ws.send(bytes([CHANNEL_STDOUT]) + payload)
                if payload == b"exit\r":
                    status = {
                        "status": "Failure",
                        "reason": "NonZeroExitCode",
                        "details": {"causes": [{"reason": "ExitCode", "message": "3"}]},
                    }
                    await ws.send(bytes([CHANNEL_STATUS]) + json.dumps(status).encode())
                    await ws.close()
                    return


async def _serve(fake: FakeApiServer) -> AsyncIterator[HttpKubernetesClient]:
    server: Server = await serve(
        fake.handle,
        "127.0.0.1",
        0,
        subprotocols=fake.subprotocols,
        process_request=fake.process_request,
    )
    port = server.sockets[0].getsockname()[1]
    http = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")
    try:
        yield HttpKubernetesClient(http, f"http://127.0.0.1:{port}", None, _Token())
    finally:
        await http.aclose()
        server.close()
        await server.wait_closed()


@pytest.fixture
async def fake_api() -> FakeApiServer:
    return FakeApiServer()


@pytest.fixture
async def exec_client(fake_api: FakeApiServer) -> AsyncIterator[HttpKubernetesClient]:
    async for client in _serve(fake_api):
        yield client


async def test_detect_shell_prefers_bash(
    exec_client: HttpKubernetesClient, fake_api: FakeApiServer
) -> None:
    shell = await exec_client.detect_shell(NAMESPACE, "app-1", "app")

    assert shell == "bash"
    query = parse_qs(urlsplit(fake_api.requests[0].path).query)
    assert query["container"] == ["app"]
    assert query["tty"] == ["false"]
    assert query["stdin"] == ["false"]
    assert query["command"][:2] == ["/bin/sh", "-c"]
    assert fake_api.requests[0].headers["Authorization"] == f"Bearer {TOKEN}"
    assert fake_api.requests[0].path.startswith(f"/api/v1/namespaces/{NAMESPACE}/pods/app-1/exec?")


async def test_detect_shell_falls_back_to_sh(
    exec_client: HttpKubernetesClient, fake_api: FakeApiServer
) -> None:
    fake_api.probe_reply = [
        bytes([CHANNEL_STDOUT]) + b"sh\n",
        bytes([CHANNEL_STATUS]) + b'{"status":"Success"}',
    ]

    assert await exec_client.detect_shell(NAMESPACE, "app-1", "app") == "sh"


async def test_detect_shell_missing_binary_raises_shell_not_found(
    exec_client: HttpKubernetesClient, fake_api: FakeApiServer
) -> None:
    status = {
        "status": "Failure",
        "message": (
            "OCI runtime exec failed: exec failed: unable to start container process: "
            'exec: "/bin/sh": stat /bin/sh: no such file or directory: unknown'
        ),
        "reason": "InternalError",
    }
    fake_api.probe_reply = [bytes([CHANNEL_STATUS]) + json.dumps(status).encode()]

    with pytest.raises(ShellNotFoundError):
        await exec_client.detect_shell(NAMESPACE, "app-1", "app")


async def test_detect_shell_exit_127_raises_shell_not_found(
    exec_client: HttpKubernetesClient, fake_api: FakeApiServer
) -> None:
    status = {
        "status": "Failure",
        "reason": "NonZeroExitCode",
        "details": {"causes": [{"reason": "ExitCode", "message": "127"}]},
    }
    fake_api.probe_reply = [bytes([CHANNEL_STATUS]) + json.dumps(status).encode()]

    with pytest.raises(ShellNotFoundError):
        await exec_client.detect_shell(NAMESPACE, "app-1", "app")


async def test_detect_shell_unknown_failure_raises_cluster_unavailable(
    exec_client: HttpKubernetesClient, fake_api: FakeApiServer
) -> None:
    fake_api.probe_reply = [bytes([CHANNEL_STATUS]) + b'{"status":"Failure","message":"boom"}']

    with pytest.raises(ClusterUnavailableError):
        await exec_client.detect_shell(NAMESPACE, "app-1", "app")


async def test_open_exec_relays_input_output_resize_and_exit(
    exec_client: HttpKubernetesClient, fake_api: FakeApiServer
) -> None:
    channel = await exec_client.open_exec(NAMESPACE, "app-1", "app", "bash", 100, 30)
    events = channel.events()
    try:
        async with asyncio.timeout(5):
            first = await anext(events)
            second = await anext(events)
            await channel.resize(120, 40)
            await channel.send_input(b"ls\r")
            echoed = await anext(events)
            await channel.send_input(b"exit\r")
            echoed_exit = await anext(events)
            exit_event = await anext(events)
    finally:
        await channel.close()

    # 빈 프레임은 건너뛰고, 두 프레임에 걸친 한 문자는 바이트 그대로 전달한다(디코딩은 Gateway 몫).
    assert [first, second] == [ExecOutput("안녕".encode()[:2]), ExecOutput("안녕".encode()[2:])]
    assert echoed == ExecOutput(b"ls\r")
    assert echoed_exit == ExecOutput(b"exit\r")
    assert isinstance(exit_event, ExecExit) and exit_event.exit_code == 3

    query = parse_qs(urlsplit(fake_api.requests[0].path).query)
    assert query["container"] == ["app"]
    assert query["tty"] == ["true"]
    assert query["stdin"] == ["true"]
    assert query["stderr"] == ["false"]
    assert query["command"] == ["/bin/sh", "-c", "TERM=xterm-256color exec bash"]
    resizes = [json.loads(payload) for ch, payload in fake_api.received if ch == CHANNEL_RESIZE]
    assert resizes == [{"Width": 100, "Height": 30}, {"Width": 120, "Height": 40}]


async def test_open_exec_connection_drop_without_status_yields_unknown_exit(
    exec_client: HttpKubernetesClient, fake_api: FakeApiServer
) -> None:
    fake_api.drop_after_greeting = True
    channel = await exec_client.open_exec(NAMESPACE, "app-1", "app", "sh", 80, 24)

    async with asyncio.timeout(5):
        events = [event async for event in channel.events()]

    assert events[-1] == ExecExit(exit_code=None)
    await channel.close()


async def test_send_after_connection_closed_is_ignored(
    exec_client: HttpKubernetesClient, fake_api: FakeApiServer
) -> None:
    fake_api.drop_after_greeting = True
    channel = await exec_client.open_exec(NAMESPACE, "app-1", "app", "sh", 80, 24)
    async with asyncio.timeout(5):
        _ = [event async for event in channel.events()]

    await channel.send_input(b"late")
    await channel.resize(10, 10)
    await channel.close()


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (HTTPStatus.NOT_FOUND, b"", PodNotFoundError),
        (HTTPStatus.CONFLICT, b"", PodNotReadyError),
        (HTTPStatus.BAD_REQUEST, b"", PodNotReadyError),
        (HTTPStatus.FORBIDDEN, b"", ClusterUnavailableError),
        (HTTPStatus.UNAUTHORIZED, b"", ClusterUnavailableError),
        (HTTPStatus.INTERNAL_SERVER_ERROR, b"", ClusterUnavailableError),
        (
            HTTPStatus.INTERNAL_SERVER_ERROR,
            b'exec: "/bin/sh": stat /bin/sh: no such file or directory',
            ShellNotFoundError,
        ),
    ],
)
async def test_open_exec_handshake_rejection_maps_to_domain_error(
    exec_client: HttpKubernetesClient,
    fake_api: FakeApiServer,
    status: HTTPStatus,
    body: bytes,
    expected: type[Exception],
) -> None:
    fake_api.reject_with = status
    fake_api.reject_body = body

    with pytest.raises(expected):
        await exec_client.open_exec(NAMESPACE, "app-1", "app", "bash", 80, 24)


async def test_open_exec_without_exec_subprotocol_raises_cluster_unavailable(
    fake_api: FakeApiServer,
) -> None:
    fake_api.subprotocols = []
    async for client in _serve(fake_api):
        with pytest.raises(ClusterUnavailableError):
            await client.open_exec(NAMESPACE, "app-1", "app", "bash", 80, 24)


async def test_open_exec_server_unreachable_raises_cluster_unavailable() -> None:
    http = httpx.AsyncClient(base_url="http://127.0.0.1:1")
    client = HttpKubernetesClient(http, "http://127.0.0.1:1", None, _Token())

    with pytest.raises(ClusterUnavailableError):
        await client.open_exec(NAMESPACE, "app-1", "app", "bash", 80, 24)
    await http.aclose()


# ---- CA ----------------------------------------------------------------------------------


def test_build_cluster_ssl_context_rejects_invalid_ca() -> None:
    with pytest.raises(NotConfiguredError):
        build_cluster_ssl_context("%%%not-base64%%%")
    with pytest.raises(NotConfiguredError):
        build_cluster_ssl_context(base64.b64encode(b"not a certificate").decode())
