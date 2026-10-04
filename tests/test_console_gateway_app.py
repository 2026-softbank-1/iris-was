"""Console Gateway 앱을 확인한다 — REST·CORS 는 ASGI 로, WebSocket 은 실제 uvicorn 소켓으로."""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import uvicorn
from httpx import ASGITransport, AsyncClient
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from app.console_gateway.main import create_app
from app.core.exceptions import ClusterUnavailableError
from tests.fakes_console_gateway import POD, FakeKubernetes, Harness, make_pod

ORIGIN = "https://app.likelion.uk"


@pytest.fixture
def harness() -> Harness:
    return Harness()


@pytest.fixture
async def client(harness: Harness) -> AsyncIterator[AsyncClient]:
    app = create_app(harness.service, [ORIGIN])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        yield http


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---- REST -----------------------------------------------------------------------------------


async def test_search_pods_returns_pods_in_api_envelope(
    client: AsyncClient, harness: Harness
) -> None:
    response = await client.get("/v1/pods", headers=_bearer(harness.ticket()))

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "data": {
            "pods": [
                {
                    "name": POD,
                    "phase": "Running",
                    "ready": True,
                    "startedAt": "2026-10-04T11:00:00Z",
                    "releaseId": 123,
                }
            ]
        },
    }


async def test_search_pods_omits_unknown_release_id() -> None:
    harness = Harness(cluster=FakeKubernetes([make_pod(release_id=None)]))
    app = create_app(harness.service, [ORIGIN])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        response = await http.get("/v1/pods", headers=_bearer(harness.ticket()))

    assert "releaseId" not in response.json()["data"]["pods"][0]


async def test_search_pods_without_ticket_returns_401(client: AsyncClient) -> None:
    response = await client.get("/v1/pods")

    assert response.status_code == 401
    assert response.json()["success"] is False
    assert response.json()["code"] == "UNAUTHORIZED"


async def test_search_pods_expired_ticket_returns_token_expired(
    client: AsyncClient, harness: Harness
) -> None:
    token = harness.ticket(now=datetime.now(UTC) - timedelta(minutes=5))

    response = await client.get("/v1/pods", headers=_bearer(token))

    assert response.status_code == 401
    assert response.json()["code"] == "TOKEN_EXPIRED"


async def test_search_pods_cluster_failure_returns_502(
    client: AsyncClient, harness: Harness
) -> None:
    async def fail(namespace: str, container: str) -> list[object]:
        raise ClusterUnavailableError("cluster request failed")

    harness.cluster.search_pods = fail  # type: ignore[method-assign]

    response = await client.get("/v1/pods", headers=_bearer(harness.ticket()))

    assert response.status_code == 502
    assert response.json()["code"] == "CLUSTER_UNAVAILABLE"


async def test_health_and_readiness_return_204(client: AsyncClient) -> None:
    assert (await client.get("/healthz")).status_code == 204
    assert (await client.get("/readyz")).status_code == 204


async def test_readiness_is_503_until_service_is_built() -> None:
    # lifespan(설정 로드)이 돌지 않은 앱이다.
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        assert (await http.get("/readyz")).status_code == 503


async def test_docs_and_schema_are_not_exposed(client: AsyncClient) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert (await client.get(path)).status_code == 404


async def test_responses_carry_request_id(client: AsyncClient) -> None:
    assert (await client.get("/healthz")).headers["X-Request-ID"]


# ---- CORS -----------------------------------------------------------------------------------


async def test_cors_preflight_from_allowed_origin(client: AsyncClient) -> None:
    response = await client.options(
        "/v1/pods",
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )

    assert response.status_code == 204
    assert response.headers["Access-Control-Allow-Origin"] == ORIGIN
    assert "Authorization" in response.headers["Access-Control-Allow-Headers"]
    assert "Access-Control-Allow-Credentials" not in response.headers


async def test_cors_headers_on_actual_response_for_allowed_origin(
    client: AsyncClient, harness: Harness
) -> None:
    response = await client.get("/v1/pods", headers={**_bearer(harness.ticket()), "Origin": ORIGIN})

    assert response.headers["Access-Control-Allow-Origin"] == ORIGIN
    assert response.headers["Vary"] == "Origin"


async def test_cors_headers_on_error_response_for_allowed_origin(client: AsyncClient) -> None:
    response = await client.get("/v1/pods", headers={"Origin": ORIGIN})

    assert response.status_code == 401
    assert response.headers["Access-Control-Allow-Origin"] == ORIGIN


@pytest.mark.parametrize("origin", ["https://evil.example", "https://app.likelion.uk.evil.example"])
async def test_cors_is_closed_for_other_origins(
    client: AsyncClient, harness: Harness, origin: str
) -> None:
    preflight = await client.options(
        "/v1/pods", headers={"Origin": origin, "Access-Control-Request-Method": "GET"}
    )
    actual = await client.get("/v1/pods", headers={**_bearer(harness.ticket()), "Origin": origin})

    assert "Access-Control-Allow-Origin" not in preflight.headers
    assert "Access-Control-Allow-Origin" not in actual.headers


# ---- WebSocket (실제 uvicorn 소켓) ------------------------------------------------------------


class LiveGateway:
    def __init__(self, harness: Harness, port: int) -> None:
        self.harness = harness
        self.port = port

    def connect(self, pod: str | None = POD, origin: str | None = ORIGIN) -> connect:
        query = f"?pod={pod}" if pod else ""
        headers = {"Origin": origin} if origin else {}
        return connect(
            f"ws://127.0.0.1:{self.port}/v1/exec{query}",
            additional_headers=headers,
            open_timeout=5,
        )


async def _start_gateway(harness: Harness) -> tuple[uvicorn.Server, asyncio.Task[None], int]:
    app = create_app(harness.service, [ORIGIN])
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
    )
    task = asyncio.create_task(server.serve())
    async with asyncio.timeout(5):
        while not server.started:  # noqa: ASYNC110 - uvicorn 은 시작 완료를 알릴 이벤트가 없다
            await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    return server, task, port


@pytest.fixture
async def live(harness: Harness) -> AsyncIterator[LiveGateway]:
    server, task, port = await _start_gateway(harness)
    try:
        yield LiveGateway(harness, port)
    finally:
        server.should_exit = True
        await task


def _auth(token: str, cols: int = 100, rows: int = 30) -> str:
    return json.dumps({"type": "auth", "token": token, "cols": cols, "rows": rows})


async def _recv(ws: ClientConnection) -> dict[str, object]:
    async with asyncio.timeout(5):
        message = await ws.recv()
    assert isinstance(message, str)
    parsed: dict[str, object] = json.loads(message)
    return parsed


async def test_exec_websocket_full_session(live: LiveGateway) -> None:
    async with live.connect() as ws:
        await ws.send(_auth(live.harness.ticket()))
        assert await _recv(ws) == {"type": "ready", "pod": POD, "shell": "bash"}

        await ws.send(json.dumps({"type": "input", "data": "ls\r"}))
        assert await _recv(ws) == {"type": "output", "data": "ls\r"}

        await ws.send(json.dumps({"type": "ping"}))
        assert await _recv(ws) == {"type": "pong"}

        await ws.send(json.dumps({"type": "resize", "cols": 120, "rows": 40}))
        await ws.send(json.dumps({"type": "input", "data": "exit\r"}))
        assert await _recv(ws) == {"type": "output", "data": "exit\r"}
        assert await _recv(ws) == {"type": "exit", "code": 3}
        with pytest.raises(ConnectionClosed):
            await _recv(ws)

    channel = live.harness.cluster.channel
    assert channel.resizes == [(120, 40)]
    assert channel.is_closed


async def test_exec_websocket_client_disconnect_closes_cluster_channel(live: LiveGateway) -> None:
    async with live.connect() as ws:
        await ws.send(_auth(live.harness.ticket()))
        assert (await _recv(ws))["type"] == "ready"

    channel = live.harness.cluster.channel
    async with asyncio.timeout(5):
        await channel.closed.wait()


async def test_exec_websocket_ignores_binary_and_malformed_frames(live: LiveGateway) -> None:
    async with live.connect() as ws:
        await ws.send(b"\x00\x01")
        await ws.send("garbage")
        await ws.send(_auth(live.harness.ticket()))
        assert (await _recv(ws))["type"] == "ready"
        await ws.send(json.dumps({"type": "input", "data": 5}))
        await ws.send(json.dumps({"type": "input", "data": "x"}))
        assert await _recv(ws) == {"type": "output", "data": "x"}


async def test_exec_websocket_invalid_ticket_sends_error_then_closes(live: LiveGateway) -> None:
    async with live.connect() as ws:
        await ws.send(_auth("garbage"))
        frame = await _recv(ws)
        with pytest.raises(ConnectionClosed):
            await _recv(ws)

    assert frame["type"] == "error"
    assert frame["code"] == "UNAUTHORIZED"
    assert frame["message"]


async def test_exec_websocket_reused_ticket_is_rejected(live: LiveGateway) -> None:
    token = live.harness.ticket()
    async with live.connect() as ws:
        await ws.send(_auth(token))
        assert (await _recv(ws))["type"] == "ready"
    async with live.connect() as ws:
        await ws.send(_auth(token))
        assert (await _recv(ws))["code"] == "TOKEN_REUSED"


async def test_exec_websocket_idle_timeout_ends_session() -> None:
    harness = Harness(idle_timeout=0.2)
    server, task, port = await _start_gateway(harness)
    try:
        live = LiveGateway(harness, port)
        async with live.connect() as ws:
            await ws.send(_auth(harness.ticket()))
            assert (await _recv(ws))["type"] == "ready"
            frame = await _recv(ws)
            with pytest.raises(ConnectionClosed):
                await _recv(ws)
    finally:
        server.should_exit = True
        await task

    assert frame["type"] == "error"
    assert frame["code"] == "IDLE_TIMEOUT"


@pytest.mark.parametrize("origin", [None, "https://evil.example"])
async def test_exec_websocket_rejects_unlisted_origin_before_upgrade(
    live: LiveGateway, origin: str | None
) -> None:
    with pytest.raises(InvalidStatus) as exc_info:
        async with live.connect(origin=origin):
            pass

    assert exc_info.value.response.status_code == 403


async def test_exec_websocket_requires_pod_query(live: LiveGateway) -> None:
    with pytest.raises(InvalidStatus):
        async with live.connect(pod=None):
            pass
