"""on-prem 콘솔 종단 — 실제 Gateway(uvicorn)가 가짜 Argo CD 터미널로 셸을 연다."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from http import HTTPStatus

import httpx
import pytest
import uvicorn
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from app.console_gateway.main import create_app
from app.core.console_ticket import (
    CONSOLE_CLUSTER_AWS,
    CONSOLE_CLUSTER_ONPREM,
    ConsoleTicketSigner,
    ConsoleTicketVerifier,
)
from app.services.console_gateway_service import (
    ConsoleGatewayService,
    ConsoleLimits,
    ConsoleSessionRegistry,
)
from tests.fakes_argocd_terminal import NAMESPACE, TOKEN, FakeArgoServer, pod_node, terminal_client
from tests.fakes_console import generate_ed25519_pem_pair

ORIGIN = "https://app.likelion.uk"
POD = "app-6d9f7c-abcde"
SERVICE_ID = 42


@dataclass
class OnpremGateway:
    argo: FakeArgoServer
    signer: ConsoleTicketSigner
    port: int

    def ticket(self, session_id: str = "s-1", cluster: str = CONSOLE_CLUSTER_ONPREM) -> str:
        token, _ = self.signer.sign(session_id, 7, SERVICE_ID, 2, cluster)
        return token

    def connect(self, pod: str = POD) -> connect:
        return connect(
            f"ws://127.0.0.1:{self.port}/v1/exec?pod={pod}",
            additional_headers={"Origin": ORIGIN},
            open_timeout=5,
        )


@asynccontextmanager
async def _gateway(argo: FakeArgoServer) -> AsyncIterator[OnpremGateway]:
    private_pem, public_pem = generate_ed25519_pem_pair()
    async with terminal_client(argo) as client:
        service = ConsoleGatewayService(
            ConsoleTicketVerifier(public_pem),
            {CONSOLE_CLUSTER_ONPREM: client},
            ConsoleSessionRegistry(3),
            ConsoleLimits(900, 3600, 3),
        )
        app = create_app(service, [ORIGIN])
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
        )
        task = asyncio.create_task(server.serve())
        async with asyncio.timeout(5):
            while not server.started:  # noqa: ASYNC110 - uvicorn 은 시작 완료를 알릴 이벤트가 없다
                await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        try:
            yield OnpremGateway(argo, ConsoleTicketSigner(private_pem), port)
        finally:
            server.should_exit = True
            await task


def _argo(*nodes: dict[str, object]) -> FakeArgoServer:
    argo = FakeArgoServer()
    argo.resource_tree = {"nodes": list(nodes) if nodes else [pod_node(POD)]}
    return argo


def _auth(token: str) -> str:
    return json.dumps({"type": "auth", "token": token, "cols": 100, "rows": 30})


async def _recv(ws: ClientConnection) -> dict[str, object]:
    async with asyncio.timeout(5):
        message = await ws.recv()
    assert isinstance(message, str)
    parsed: dict[str, object] = json.loads(message)
    return parsed


async def _error_code(
    gateway: OnpremGateway, *, pod: str = POD, ticket: str | None = None
) -> object:
    async with gateway.connect(pod) as ws:
        await ws.send(_auth(ticket or gateway.ticket()))
        frame = await _recv(ws)
        assert frame["type"] == "error", frame
        return frame["code"]


async def test_onprem_pods_come_from_argocd_resource_tree() -> None:
    async with _gateway(_argo(pod_node(POD), pod_node("other", reason="Pending"))) as gateway:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{gateway.port}") as http:
            response = await http.get(
                "/v1/pods", headers={"Authorization": f"Bearer {gateway.ticket()}"}
            )

    assert response.status_code == 200
    pods = response.json()["data"]["pods"]
    assert [(pod["name"], pod["ready"]) for pod in pods] == [(POD, True), ("other", False)]
    # 리소스 트리에는 release 정보가 없다.
    assert all("releaseId" not in pod for pod in pods)
    (rest_request,) = gateway.argo.rest_requests
    assert rest_request.path.startswith(f"/api/v1/applications/{NAMESPACE}/resource-tree")


async def test_onprem_exec_full_session_through_argocd_terminal() -> None:
    async with _gateway(_argo()) as gateway:
        async with gateway.connect() as ws:
            await ws.send(_auth(gateway.ticket()))
            # 셸은 Argo CD 가 고르므로 ready 에 shell 이 없다.
            assert await _recv(ws) == {"type": "ready", "pod": POD}
            assert await _recv(ws) == {"type": "output", "data": "root@app:/app# "}

            await ws.send(json.dumps({"type": "input", "data": "ls\r"}))
            assert await _recv(ws) == {"type": "output", "data": "ls\r"}

            await ws.send(json.dumps({"type": "resize", "cols": 120, "rows": 40}))
            await ws.send(json.dumps({"type": "ping"}))
            assert await _recv(ws) == {"type": "pong"}

            await ws.send(json.dumps({"type": "input", "data": "exit\r"}))
            assert await _recv(ws) == {"type": "output", "data": "exit\r"}
            # Argo CD 는 종료 코드를 알려 주지 않는다.
            assert await _recv(ws) == {"type": "exit"}
            with pytest.raises(ConnectionClosed):
                await _recv(ws)

    (request,) = gateway.argo.requests
    # namespace·컨테이너·Application 은 ticket 에서 정해진다. 요청의 pod 만 따른다.
    assert f"appName={NAMESPACE}" in request.path
    assert f"namespace={NAMESPACE}" in request.path
    assert "container=app" in request.path
    assert f"pod={POD}" in request.path
    assert gateway.argo.received == [
        {"operation": "resize", "cols": 100, "rows": 30},
        {"operation": "stdin", "data": "ls\r"},
        {"operation": "resize", "cols": 120, "rows": 40},
        {"operation": "stdin", "data": "exit\r"},
    ]


async def test_onprem_ticket_for_unconfigured_cluster_is_cluster_unavailable() -> None:
    async with _gateway(_argo()) as gateway:
        code = await _error_code(gateway, ticket=gateway.ticket(cluster=CONSOLE_CLUSTER_AWS))

    assert code == "CLUSTER_UNAVAILABLE"
    assert gateway.argo.requests == []


async def test_onprem_pod_missing_from_tree_is_pod_not_found() -> None:
    async with _gateway(_argo(pod_node("someone-else"))) as gateway:
        code = await _error_code(gateway)

    assert code == "POD_NOT_FOUND"
    assert gateway.argo.requests == []


async def test_onprem_not_ready_pod_is_pod_not_ready() -> None:
    async with _gateway(_argo(pod_node(POD, reason="Pending", health="Progressing"))) as gateway:
        code = await _error_code(gateway)

    assert code == "POD_NOT_READY"
    assert gateway.argo.requests == []


async def test_onprem_argocd_pod_rejection_is_pod_not_found() -> None:
    argo = _argo()
    argo.reject_with = HTTPStatus.BAD_REQUEST
    argo.reject_body = "Pod doesn't belong to specified app"
    async with _gateway(argo) as gateway:
        code = await _error_code(gateway)

    assert code == "POD_NOT_FOUND"


@pytest.mark.parametrize(
    ("status", "body"),
    [(HTTPStatus.UNAUTHORIZED, "permission denied"), (HTTPStatus.NOT_FOUND, "")],
)
async def test_onprem_argocd_exec_disabled_or_unauthorized_is_cluster_unavailable(
    status: HTTPStatus, body: str
) -> None:
    argo = _argo()
    argo.reject_with = status
    argo.reject_body = body
    async with _gateway(argo) as gateway:
        code = await _error_code(gateway)

    assert code == "CLUSTER_UNAVAILABLE"


async def test_onprem_argocd_closing_before_output_is_shell_not_found() -> None:
    argo = _argo()
    argo.scenario = "close_at_once"
    async with _gateway(argo) as gateway:
        code = await _error_code(gateway)

    assert code == "SHELL_NOT_FOUND"


async def test_onprem_token_never_reaches_the_browser() -> None:
    async with _gateway(_argo()) as gateway:
        async with gateway.connect() as ws:
            await ws.send(_auth(gateway.ticket()))
            frames = [await _recv(ws), await _recv(ws)]

    assert TOKEN not in json.dumps(frames)
