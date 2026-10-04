import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from http import HTTPStatus
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import SecretStr

from app.clients import argocd_terminal_client
from app.clients.argocd_terminal_client import ArgoCdTerminalClient
from app.clients.kubernetes_client import ExecExit, ExecOutput
from app.core.exceptions import (
    ClusterUnavailableError,
    PodNotFoundError,
    PodNotReadyError,
    ShellNotFoundError,
)
from tests.fakes_argocd_terminal import (
    NAMESPACE,
    TOKEN,
    FakeArgoServer,
    pod_node,
    terminal_client,
)


def _rest_client(handler: Callable[[httpx.Request], httpx.Response]) -> ArgoCdTerminalClient:
    http = httpx.AsyncClient(base_url="https://argocd.test", transport=httpx.MockTransport(handler))
    return ArgoCdTerminalClient(
        http,
        "https://argocd.test",
        SecretStr(TOKEN),
        None,
        project="iris-svc-project",
        app_namespace="argocd",
    )


# ---- Pod 조회 (resource-tree) ---------------------------------------------------------------


async def test_search_pods_maps_pod_nodes_newest_first() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "nodes": [
                    pod_node("old", created_at="2026-10-04T10:00:00Z"),
                    pod_node("new", created_at="2026-10-04T11:00:00Z"),
                    pod_node(
                        "crashing",
                        reason="CrashLoopBackOff",
                        health="Degraded",
                        created_at="2026-10-04T10:30:00Z",
                    ),
                    pod_node("no-time", created_at=None),
                    # 다른 종류·다른 namespace·group 이 있는 노드는 Pod 가 아니다.
                    pod_node("rs", kind="ReplicaSet", group="apps"),
                    pod_node("elsewhere", namespace="svc-99"),
                    pod_node("grouped", group="example.io"),
                    "garbage",
                ]
            },
        )

    pods = await _rest_client(handler).search_pods(NAMESPACE, "app")

    assert [pod.name for pod in pods] == ["new", "crashing", "old", "no-time"]
    assert pods[0].started_at == datetime(2026, 10, 4, 11, 0, tzinfo=UTC)
    assert pods[3].started_at is None
    assert [pod.is_ready for pod in pods] == [True, False, True, True]
    assert pods[1].phase == "CrashLoopBackOff"
    # 리소스 트리 노드에는 라벨이 없어 release 를 모른다.
    assert all(pod.release_id is None for pod in pods)
    (request,) = seen
    assert request.url.path == f"/api/v1/applications/{NAMESPACE}/resource-tree"
    assert dict(request.url.params) == {"appNamespace": "argocd"}
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"


async def test_search_pods_terminating_or_unhealthy_pod_is_not_ready() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "nodes": [
                    pod_node("going", reason="Terminating", health="Healthy"),
                    pod_node("progressing", reason="Running", health="Progressing"),
                    pod_node("no-health", reason="Running", health=None),
                    pod_node("no-info", reason=None, health="Healthy"),
                ]
            },
        )

    pods = {pod.name: pod for pod in await _rest_client(handler).search_pods(NAMESPACE, "app")}

    assert not any(pod.is_ready for pod in pods.values())
    assert pods["going"].phase == "Terminating"
    assert pods["no-info"].phase == "Unknown"


@pytest.mark.parametrize("status_code", [403, 404])
async def test_search_pods_hidden_application_returns_empty(status_code: int) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": "permission denied"})

    assert await _rest_client(handler).search_pods(NAMESPACE, "app") == []


async def test_search_pods_without_nodes_returns_empty() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    assert await _rest_client(handler).search_pods(NAMESPACE, "app") == []


@pytest.mark.parametrize("status_code", [401, 500, 502])
async def test_search_pods_server_error_raises_cluster_unavailable(status_code: int) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": "secret detail"})

    with pytest.raises(ClusterUnavailableError) as exc_info:
        await _rest_client(handler).search_pods(NAMESPACE, "app")

    assert "secret detail" not in exc_info.value.message
    assert TOKEN not in exc_info.value.message


async def test_search_pods_network_error_raises_cluster_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(ClusterUnavailableError):
        await _rest_client(handler).search_pods(NAMESPACE, "app")


async def test_search_pods_invalid_json_raises_cluster_unavailable() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    with pytest.raises(ClusterUnavailableError):
        await _rest_client(handler).search_pods(NAMESPACE, "app")


async def test_find_pod_returns_named_pod_or_none() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"nodes": [pod_node("here")]})

    client = _rest_client(handler)

    found = await client.find_pod(NAMESPACE, "here", "app")
    assert found is not None
    assert found.name == "here"
    assert await client.find_pod(NAMESPACE, "gone", "app") is None


async def test_search_pods_escapes_application_name() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(404)

    await _rest_client(handler).search_pods("../projects", "app")

    assert seen[0].url.raw_path.decode().startswith("/api/v1/applications/..%2Fprojects/")


async def test_detect_shell_is_left_to_argocd() -> None:
    client = _rest_client(lambda _: httpx.Response(200, json={}))

    assert await client.detect_shell(NAMESPACE, "pod", "app") is None


# ---- 터미널 (가짜 Argo CD WebSocket 서버) ----------------------------------------------


async def test_open_exec_sends_argocd_query_and_cookie_then_resizes() -> None:
    fake = FakeArgoServer()
    async with terminal_client(fake) as client:
        channel = await client.open_exec(NAMESPACE, "app-6cd8-abcde", "app", None, 120, 30)
        await channel.close()

    (request,) = fake.requests
    split = urlsplit(request.path)
    assert split.path == "/terminal"
    assert {key: values[0] for key, values in parse_qs(split.query).items()} == {
        "pod": "app-6cd8-abcde",
        "container": "app",
        "appName": NAMESPACE,
        "appNamespace": "argocd",
        "projectName": "iris-svc-project",
        "namespace": NAMESPACE,
    }
    # Argo CD 는 JWT 를 쿠키에서만 읽는다. 토큰이 URL 에 들어가면 안 된다.
    assert request.headers["Cookie"] == f"argocd.token={TOKEN}"
    assert TOKEN not in request.path
    assert fake.received[0] == {"operation": "resize", "cols": 120, "rows": 30}


async def test_open_exec_relays_input_output_and_exit() -> None:
    fake = FakeArgoServer()
    # 제어 프레임·바이너리·operation 없는 프레임은 출력이 아니다.
    fake.extra_frames = [json.dumps({"Code": 1}), b"\x00binary", "not json"]
    async with terminal_client(fake) as client:
        channel = await client.open_exec(NAMESPACE, "pod", "app", None, 80, 24)
        events = channel.events()
        first = await anext(events)
        await channel.resize(100, 40)
        await channel.send_input("ls 안녕\r".encode())
        echoed = await anext(events)
        await channel.send_input(b"exit\r")
        exit_echo = await anext(events)
        closed = await anext(events)
        await channel.close()

    assert first == ExecOutput(b"root@app:/app# ")
    assert echoed == ExecOutput("ls 안녕\r".encode())
    assert exit_echo == ExecOutput(b"exit\r")
    # 셸이 끝나면 Argo CD 가 종료 코드 없이 연결을 닫는다.
    assert closed == ExecExit(exit_code=None)
    assert fake.received[1] == {"operation": "resize", "cols": 100, "rows": 40}
    assert fake.received[2] == {"operation": "stdin", "data": "ls 안녕\r"}


async def test_open_exec_closed_before_first_output_raises_shell_not_found(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = FakeArgoServer()
    fake.scenario = "close_at_once"
    async with terminal_client(fake) as client:
        with pytest.raises(ShellNotFoundError) as exc_info:
            await client.open_exec(NAMESPACE, "pod", "app", None, 80, 24)

    assert exc_info.value.code == "SHELL_NOT_FOUND"
    # 셸이 없는 이미지와 서버 SA 의 exec 권한 부족을 구분할 수 없어 운영자용 로그를 남긴다.
    assert any(getattr(record, "argo_closed_before_output", False) for record in caplog.records)
    assert TOKEN not in caplog.text


async def test_open_exec_silent_shell_is_opened_after_first_output_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(argocd_terminal_client, "FIRST_OUTPUT_TIMEOUT_SECONDS", 0.2)
    fake = FakeArgoServer()
    fake.scenario = "silent"
    async with terminal_client(fake) as client:
        channel = await asyncio.wait_for(
            client.open_exec(NAMESPACE, "pod", "app", None, 80, 24), timeout=3
        )
        await channel.send_input(b"id\r")
        event = await anext(channel.events())
        await channel.close()

    assert event == ExecOutput(b"id\r")


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (HTTPStatus.BAD_REQUEST, "Pod doesn't belong to specified app", PodNotFoundError),
        (HTTPStatus.BAD_REQUEST, "Cannot find pod", PodNotFoundError),
        (HTTPStatus.BAD_REQUEST, "container find running", PodNotReadyError),
        (HTTPStatus.NOT_FOUND, "App not found", PodNotFoundError),
        # exec.enabled 가 꺼져 있으면 본문 없는 404 다.
        (HTTPStatus.NOT_FOUND, "", ClusterUnavailableError),
        (HTTPStatus.BAD_REQUEST, "Missing required parameters", ClusterUnavailableError),
        (HTTPStatus.UNAUTHORIZED, "permission denied: exec, create", ClusterUnavailableError),
        (HTTPStatus.INTERNAL_SERVER_ERROR, "Cannot get app", ClusterUnavailableError),
    ],
)
async def test_open_exec_handshake_rejection_is_mapped(
    status: HTTPStatus, body: str, expected: type[Exception]
) -> None:
    fake = FakeArgoServer()
    fake.reject_with = status
    fake.reject_body = body
    async with terminal_client(fake) as client:
        with pytest.raises(expected) as exc_info:
            await client.open_exec(NAMESPACE, "pod", "app", None, 80, 24)

    assert TOKEN not in str(exc_info.value)


async def test_open_exec_wrong_token_is_cluster_unavailable() -> None:
    fake = FakeArgoServer()
    async with terminal_client(fake) as client:
        wrong = ArgoCdTerminalClient(
            client._http,
            client._base_url,
            SecretStr("another-token"),
            None,
            project="iris-svc-project",
            app_namespace="argocd",
        )
        with pytest.raises(ClusterUnavailableError):
            await wrong.open_exec(NAMESPACE, "pod", "app", None, 80, 24)


async def test_open_exec_connection_refused_raises_cluster_unavailable() -> None:
    async with httpx.AsyncClient(base_url="http://127.0.0.1:1") as http:
        client = ArgoCdTerminalClient(
            http,
            "http://127.0.0.1:1",
            SecretStr(TOKEN),
            None,
            project="iris-svc-project",
            app_namespace="argocd",
        )
        with pytest.raises(ClusterUnavailableError) as exc_info:
            await client.open_exec(NAMESPACE, "pod", "app", None, 80, 24)

    assert TOKEN not in str(exc_info.value)


async def test_token_whitespace_from_secret_files_is_stripped() -> None:
    fake = FakeArgoServer()
    async with terminal_client(fake) as client:
        padded = ArgoCdTerminalClient(
            client._http,
            client._base_url,
            SecretStr(f"{TOKEN}\n"),
            None,
            project="iris-svc-project",
            app_namespace="argocd",
        )
        channel = await padded.open_exec(NAMESPACE, "pod", "app", None, 80, 24)
        await channel.close()

    assert fake.requests[0].headers["Cookie"] == f"argocd.token={TOKEN}"


async def test_missing_cookie_is_cluster_unavailable() -> None:
    # 쿠키가 없을 때 Argo CD 는 400 `Auth cookie not found` 로 답한다.
    fake = FakeArgoServer()
    fake.reject_with = HTTPStatus.BAD_REQUEST
    fake.reject_body = "Auth cookie not found"
    async with terminal_client(fake) as client:
        with pytest.raises(ClusterUnavailableError):
            await client.open_exec(NAMESPACE, "pod", "app", None, 80, 24)
