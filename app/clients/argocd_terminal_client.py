"""Argo CD 터미널 Client — on-prem 타깃의 Pod 조회와 셸. Console Gateway 만 쓴다(ADR 0035).

on-prem 서버 클러스터에는 Argo CD 만 닿는다. Pod 목록은 Argo CD REST(`resource-tree`)로, 셸은
Argo CD 내장 터미널(`GET /terminal`, WebSocket)로 연다. 프로토콜은 Argo CD v3.5.3 소스에서 확인했다.

- 터미널은 JWT 를 쿠키 `argocd.token` 에서만 읽는다(`Authorization` 헤더는 보지 않는다).
- 메시지는 JSON 텍스트 프레임이다. 보내는 쪽 `{"operation": "stdin", "data": …}`·
  `{"operation": "resize", "cols": …, "rows": …}`, 받는 쪽 `{"operation": "stdout", "data": …}`.
- 셸은 Argo CD 가 `exec.shells` 순서로 시도해 고르고, 모두 실패하면 연결을 닫는다. 종료 코드는
  알려 주지 않는다.

Application 이름은 namespace 와 같은 `svc-{id}` 다. 클러스터 호출 실패는 모두
ClusterUnavailableError 로 바꾸고 응답 타입(JSON·프레임)은 이 모듈 밖으로 나가지 않는다.
"""

import asyncio
import json
import logging
import ssl
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from pydantic import SecretStr
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from app.clients.kubernetes_client import (
    CLUSTER_PING_INTERVAL_SECONDS,
    CONNECT_TIMEOUT_SECONDS,
    ExecChannel,
    ExecEvent,
    ExecExit,
    ExecOutput,
    PodInfo,
    Shell,
)
from app.core.exceptions import (
    ClusterUnavailableError,
    PodNotFoundError,
    PodNotReadyError,
    ShellNotFoundError,
)

logger = logging.getLogger(__name__)

# Argo CD 가 인증 JWT 를 읽는 쿠키 이름(common.AuthCookieName).
ARGOCD_AUTH_COOKIE = "argocd.token"
# 연결한 뒤 첫 출력(셸 프롬프트)을 기다리는 시간. 첫 출력 없이 연결이 끊기면 셸을 못 연 것이다.
FIRST_OUTPUT_TIMEOUT_SECONDS = 5.0
_POD_KIND = "Pod"
_STATUS_REASON = "Status Reason"
_RUNNING = "Running"
_HEALTHY = "Healthy"


class ArgoCdTerminalClient:
    """`KubernetesClient` Protocol 을 Argo CD 로 구현한다. namespace 가 곧 Application 이름이다."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        base_url: str,
        token: SecretStr,
        ssl_context: ssl.SSLContext | None,
        *,
        project: str,
        app_namespace: str,
    ) -> None:
        # http 는 base_url 로 만든 클라이언트다(운영 설정은 https 만 받는다). ssl_context 없이
        # `http://…` 를 주는 것은 로컬 테스트용 가짜 서버뿐이다.
        self._http = http
        self._base_url = base_url.rstrip("/")
        # 환경변수·Secret 파일에서 온 값의 끝 개행이 헤더·쿠키를 깨지 않게 뺀다.
        self._token = SecretStr(token.get_secret_value().strip())
        self._ssl_context = ssl_context
        self._project = project
        self._app_namespace = app_namespace

    async def search_pods(self, namespace: str, container: str) -> list[PodInfo]:
        """Application 의 Pod. 리소스 트리에는 컨테이너 이름이 없어 container 는 거르지 않는다."""
        nodes = await self._search_pod_nodes(namespace)
        pods = [info for node in nodes if (info := _to_pod_info(node, namespace)) is not None]
        return sorted(pods, key=lambda pod: pod.started_at or _EPOCH, reverse=True)

    async def find_pod(self, namespace: str, name: str, container: str) -> PodInfo | None:
        for pod in await self.search_pods(namespace, container):
            if pod.name == name:
                return pod
        return None

    async def detect_shell(self, namespace: str, pod: str, container: str) -> Shell | None:
        # Argo CD 가 셸을 고른다. 연결한 뒤에야 알 수 있고 그것도 알려 주지 않는다.
        return None

    async def open_exec(
        self, namespace: str, pod: str, container: str, shell: Shell | None, cols: int, rows: int
    ) -> ExecChannel:
        query = urlencode(
            {
                "pod": pod,
                "container": container,
                "appName": namespace,
                "appNamespace": self._app_namespace,
                "projectName": self._project,
                "namespace": namespace,
            }
        )
        is_tls = self._base_url.startswith("https://")
        ws_base = ("wss://" if is_tls else "ws://") + self._base_url.split("://", 1)[1]
        # 쿠키 값에 토큰이 들어간다. 로그·예외에 URI 만 쓰고 헤더는 쓰지 않는다.
        headers = {"Cookie": f"{ARGOCD_AUTH_COOKIE}={self._token.get_secret_value()}"}
        try:
            ws = await connect(
                f"{ws_base}/terminal?{query}",
                ssl=self._ssl_context if is_tls else None,
                additional_headers=headers,
                open_timeout=CONNECT_TIMEOUT_SECONDS,
                ping_interval=CLUSTER_PING_INTERVAL_SECONDS,
                ping_timeout=None,
            )
        except InvalidStatus as exc:
            raise _map_handshake_status(exc, pod) from exc
        except (InvalidHandshake, OSError, TimeoutError) as exc:
            raise ClusterUnavailableError("argocd terminal connection failed") from exc
        channel = _ArgoTerminalChannel(ws)
        await channel.resize(cols, rows)
        if not await channel.wait_first_output():
            await channel.close()
            # 셸이 없는 이미지와 서버 SA 에 pods/exec 가 없는 경우를 Argo CD 가 구분해 주지 않는다.
            logger.warning(
                "argocd terminal closed before output",
                extra={"action": "open_exec", "pod": pod, "argo_closed_before_output": True},
            )
            raise ShellNotFoundError("argocd could not start a shell", pod=pod)
        return channel

    async def _search_pod_nodes(self, application: str) -> list[dict[str, Any]]:
        headers = {"Authorization": f"Bearer {self._token.get_secret_value()}"}
        path = f"/api/v1/applications/{quote(application, safe='')}/resource-tree"
        try:
            response = await self._http.get(
                path, params={"appNamespace": self._app_namespace}, headers=headers
            )
        except httpx.HTTPError as exc:
            raise ClusterUnavailableError("argocd request failed") from exc
        # 아직 배포되지 않은 서비스는 Application 이 없다. Argo 는 이를 403 으로 숨길 수 있다.
        if response.status_code in (403, 404):
            return []
        if response.status_code != 200:
            raise ClusterUnavailableError("argocd rejected request", status=response.status_code)
        try:
            nodes = response.json().get("nodes") or []
        except (ValueError, AttributeError) as exc:
            raise ClusterUnavailableError("invalid argocd response") from exc
        return [node for node in nodes if isinstance(node, dict)]


class _ArgoTerminalChannel:
    def __init__(self, ws: ClientConnection) -> None:
        self._ws = ws
        # 첫 출력을 기다리며 미리 받은 출력. events() 가 가장 먼저 내보낸다.
        self._buffered: list[bytes] = []

    async def wait_first_output(self) -> bool:
        """첫 출력을 기다린다. 받았거나 말없는 셸이면 True, 그 전에 연결이 끊기면 False 다."""
        try:
            async with asyncio.timeout(FIRST_OUTPUT_TIMEOUT_SECONDS):
                while True:
                    data = _parse_stdout(await self._ws.recv())
                    if data is not None:
                        self._buffered.append(data)
                        return True
        except TimeoutError:
            return True
        except ConnectionClosed:
            return False

    async def send_input(self, data: bytes) -> None:
        await self._send({"operation": "stdin", "data": data.decode("utf-8", errors="replace")})

    async def resize(self, cols: int, rows: int) -> None:
        await self._send({"operation": "resize", "cols": cols, "rows": rows})

    async def _send(self, payload: dict[str, Any]) -> None:
        try:
            await self._ws.send(json.dumps(payload))
        except ConnectionClosed:
            # 연결이 이미 끝났다. 끝났다는 사실은 events() 가 ExecExit 로 알린다.
            return

    async def events(self) -> AsyncIterator[ExecEvent]:
        buffered, self._buffered = self._buffered, []
        for early in buffered:
            yield ExecOutput(early)
        try:
            async for message in self._ws:
                data = _parse_stdout(message)
                if data is not None:
                    yield ExecOutput(data)
        except ConnectionClosed:
            pass
        # Argo CD 는 셸이 끝나면 종료 코드 없이 연결을 닫는다.
        yield ExecExit(exit_code=None)

    async def close(self) -> None:
        await self._ws.close()


_EPOCH = datetime.fromtimestamp(0, UTC)


def _parse_stdout(message: str | bytes) -> bytes | None:
    """`{"operation": "stdout", "data": …}` 의 출력 바이트. 그 밖의 프레임(제어 코드 등)은 None."""
    if isinstance(message, bytes):
        return None
    try:
        frame = json.loads(message)
    except ValueError:
        return None
    if not isinstance(frame, dict) or frame.get("operation") != "stdout":
        return None
    data = frame.get("data")
    return data.encode() if isinstance(data, str) else None


def _to_pod_info(node: dict[str, Any], namespace: str) -> PodInfo | None:
    if node.get("kind") != _POD_KIND or node.get("group") or node.get("namespace") != namespace:
        return None
    name = node.get("name")
    if not isinstance(name, str) or not name:
        return None
    info = {
        item.get("name"): item.get("value")
        for item in node.get("info") or []
        if isinstance(item, dict)
    }
    phase = info.get(_STATUS_REASON)
    phase = phase if isinstance(phase, str) and phase else "Unknown"
    health = node.get("health")
    health_status = health.get("status") if isinstance(health, dict) else None
    return PodInfo(
        name=name,
        phase=phase,
        is_ready=health_status == _HEALTHY and phase == _RUNNING,
        started_at=_parse_time(node.get("createdAt")),
        # 리소스 트리의 Pod 노드에는 라벨이 없어 release 를 알 수 없다.
        release_id=None,
    )


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _map_handshake_status(exc: InvalidStatus, pod: str) -> Exception:
    """핸드셰이크 거절을 Gateway 오류로 바꾼다. Argo CD 의 본문 문구는 v3.5.3 소스 기준이다."""
    status_code = exc.response.status_code
    body = exc.response.body.decode("utf-8", "ignore").lower() if exc.response.body else ""
    if status_code == 400 and ("doesn't belong" in body or "cannot find pod" in body):
        return PodNotFoundError("pod not found", pod=pod)
    if status_code == 400 and "container find running" in body:
        return PodNotReadyError("pod is not ready", pod=pod)
    if status_code == 404 and "app not found" in body:
        return PodNotFoundError("application not found", pod=pod)
    # 401 은 토큰이 틀렸거나 role 에 exec·applications 권한이 없다.
    # 본문 없는 404 는 exec.enabled 가 꺼져 있다.
    return ClusterUnavailableError("argocd rejected terminal", status=status_code)
