"""Argo CD 터미널 테스트용 가짜 서버. v3.5.3 `/terminal`·`resource-tree` 의 모양을 흉내 낸다."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http import HTTPStatus
from typing import Any

import httpx
from pydantic import SecretStr
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.http11 import Request, Response

from app.clients.argocd_terminal_client import ArgoCdTerminalClient

NAMESPACE = "svc-42"
TOKEN = "argocd-role-token-must-not-leak"


def pod_node(
    name: str,
    *,
    namespace: str = NAMESPACE,
    reason: str | None = "Running",
    health: str | None = "Healthy",
    created_at: str | None = "2026-10-04T11:00:00Z",
    kind: str = "Pod",
    group: str | None = None,
) -> dict[str, Any]:
    node: dict[str, Any] = {
        "version": "v1",
        "kind": kind,
        "namespace": namespace,
        "name": name,
        "uid": f"uid-{name}",
        "info": [
            {"name": "Node", "value": "iris-onprem-01"},
            {"name": "Containers", "value": "1/1"},
        ],
    }
    if group is not None:
        node["group"] = group
    if reason is not None:
        node["info"].append({"name": "Status Reason", "value": reason})
    if health is not None:
        node["health"] = {"status": health}
    if created_at is not None:
        node["createdAt"] = created_at
    return node


class FakeArgoServer:
    """Argo CD v3.5.3 `/terminal` 처럼 말하는 가짜 서버. 요청을 기록하고 시나리오대로 답한다."""

    def __init__(self) -> None:
        self.requests: list[Request] = []
        self.received: list[dict[str, Any]] = []
        self.reject_with: HTTPStatus | None = None
        self.reject_body = ""
        # 시나리오: greet(프롬프트를 보낸다) · close_at_once(출력 없이 닫는다) · silent(말없이 연다)
        self.scenario = "greet"
        self.extra_frames: list[str | bytes] = []
        # REST(`resource-tree`) 응답과 요청 기록.
        self.resource_tree: dict[str, Any] = {"nodes": []}
        self.rest_requests: list[Request] = []

    async def process_request(
        self, connection: ServerConnection, request: Request
    ) -> Response | None:
        if request.path.startswith("/api/v1/applications/"):
            self.rest_requests.append(request)
            if request.headers.get("Authorization") != f"Bearer {TOKEN}":
                return connection.respond(HTTPStatus.UNAUTHORIZED, "unauthorized")
            return connection.respond(HTTPStatus.OK, json.dumps(self.resource_tree))
        self.requests.append(request)
        cookie = request.headers.get("Cookie", "")
        if "argocd.token=" not in cookie:
            return connection.respond(HTTPStatus.BAD_REQUEST, "Auth cookie not found")
        if f"argocd.token={TOKEN}" not in cookie.split("; "):
            return connection.respond(HTTPStatus.UNAUTHORIZED, "Invalid token")
        if self.reject_with is not None:
            return connection.respond(self.reject_with, self.reject_body)
        return None

    async def handle(self, ws: ServerConnection) -> None:
        if self.scenario == "close_at_once":
            await ws.close()
            return
        if self.scenario == "greet":
            for frame in self.extra_frames:
                await ws.send(frame)
            await ws.send(json.dumps({"operation": "stdout", "data": "root@app:/app# "}))
        async for message in ws:
            assert isinstance(message, str)
            frame = json.loads(message)
            self.received.append(frame)
            if frame["operation"] == "stdin":
                await ws.send(json.dumps({"operation": "stdout", "data": frame["data"]}))
                if frame["data"] == "exit\r":
                    await ws.close()
                    return


@asynccontextmanager
async def terminal_client(fake: FakeArgoServer) -> AsyncIterator[ArgoCdTerminalClient]:
    server: Server = await serve(fake.handle, "127.0.0.1", 0, process_request=fake.process_request)
    port = server.sockets[0].getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    async with httpx.AsyncClient(base_url=base_url) as http:
        try:
            yield ArgoCdTerminalClient(
                http,
                base_url,
                SecretStr(TOKEN),
                None,
                project="iris-svc-project",
                app_namespace="argocd",
            )
        finally:
            server.close()
            await server.wait_closed()
