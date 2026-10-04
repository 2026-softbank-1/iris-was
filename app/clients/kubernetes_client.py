"""Prod 클러스터 API Client — Pod 조회와 `pods/exec`. Console Gateway 만 쓴다(ADR 0033).

Pod 조회는 `httpx`, exec 는 `v4.channel.k8s.io` WebSocket 프로토콜을 `websockets` 로 직접 다룬다.
메시지는 첫 바이트가 채널 번호다: 0 stdin · 1 stdout · 2 stderr · 3 상태(JSON) · 4 터미널 크기.
클러스터 호출 실패는 모두 ClusterUnavailableError 로 바꾼다. 응답 타입(JSON·프레임)은 이 모듈
밖으로 나가지 않는다.
"""

import asyncio
import base64
import binascii
import json
import logging
import ssl
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from urllib.parse import quote, urlencode

import httpx
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus
from websockets.typing import Subprotocol

from app.core.exceptions import (
    ClusterUnavailableError,
    NotConfiguredError,
    PodNotFoundError,
    PodNotReadyError,
    ShellNotFoundError,
)

logger = logging.getLogger(__name__)

Shell = Literal["bash", "sh"]

EXEC_SUBPROTOCOL = "v4.channel.k8s.io"
CHANNEL_STDIN = 0
CHANNEL_STDOUT = 1
CHANNEL_STDERR = 2
CHANNEL_STATUS = 3
CHANNEL_RESIZE = 4

# Deploy Worker 가 chart 로 Pod 에 넣는 release 라벨(OTel 이 `iris_release_id` 로 올리는 그 값).
RELEASE_LABEL = "iris/release-id"
CONNECT_TIMEOUT_SECONDS = 10.0
SHELL_PROBE_TIMEOUT_SECONDS = 10.0
# 인프라(NLB 등)의 유휴 연결 정리를 피하려는 WebSocket ping. 응답이 없어도 끊지 않는다.
CLUSTER_PING_INTERVAL_SECONDS = 30.0
_SHELL_PROBE_COMMAND = [
    "/bin/sh",
    "-c",
    "if command -v bash >/dev/null 2>&1; then echo bash; else echo sh; fi",
]
# 이미지에 `/bin/sh` 가 없을 때 컨테이너 런타임이 돌려주는 메시지의 조각(소문자).
_MISSING_EXECUTABLE_MARKERS = (
    "no such file or directory",
    "executable file not found",
    "oci runtime exec failed",
)
_MISSING_EXECUTABLE_EXIT_CODES = frozenset({126, 127})


@dataclass(frozen=True)
class PodInfo:
    name: str
    # Pod 단계(Pending·Running·…). 삭제 중이면 Terminating 이다.
    phase: str
    # 컨테이너가 준비됐고 Pod 가 Running 이다. 연결할 수 있는 Pod 만 True 다.
    is_ready: bool
    started_at: datetime | None
    release_id: int | None


@dataclass(frozen=True)
class ExecOutput:
    """TTY 의 출력 바이트(stdout 과 stderr 가 합쳐진다)."""

    data: bytes


@dataclass(frozen=True)
class ExecExit:
    """exec 가 끝났다. 종료 코드를 모르면 None 이다(연결이 상태 없이 끊긴 경우 등)."""

    exit_code: int | None
    message: str | None = None


ExecEvent = ExecOutput | ExecExit


class ExecChannel(Protocol):
    async def send_input(self, data: bytes) -> None: ...

    async def resize(self, cols: int, rows: int) -> None: ...

    def events(self) -> AsyncIterator[ExecEvent]:
        """출력과 종료 이벤트. `ExecExit` 를 끝으로 반복이 끝난다."""
        ...

    async def close(self) -> None: ...


class KubernetesClient(Protocol):
    async def search_pods(self, namespace: str, container: str) -> list[PodInfo]:
        """namespace 의 Pod 중 container 가 있는 것. startedAt 내림차순."""
        ...

    async def find_pod(self, namespace: str, name: str, container: str) -> PodInfo | None: ...

    async def detect_shell(self, namespace: str, pod: str, container: str) -> Shell:
        """컨테이너에서 쓸 셸. `/bin/sh` 가 없으면 ShellNotFoundError."""
        ...

    async def open_exec(
        self, namespace: str, pod: str, container: str, shell: Shell, cols: int, rows: int
    ) -> ExecChannel: ...


class ClusterTokenProvider(Protocol):
    async def get_token(self) -> str:
        """클러스터 API 의 bearer 토큰."""
        ...


def build_cluster_ssl_context(ca_base64: str) -> ssl.SSLContext:
    """클러스터 API 서버 인증서의 CA(PEM 을 base64 로 인코딩한 값)로 TLS 검증 컨텍스트를 만든다."""
    try:
        pem = base64.b64decode(ca_base64, validate=True).decode("ascii")
        return ssl.create_default_context(cadata=pem)
    except (binascii.Error, ValueError, ssl.SSLError) as exc:
        raise NotConfiguredError("cluster ca is invalid", setting="CONSOLE_AWS_CLUSTER_CA") from exc


class HttpKubernetesClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        endpoint: str,
        ssl_context: ssl.SSLContext | None,
        token_provider: ClusterTokenProvider,
    ) -> None:
        # http 는 같은 CA 로 TLS 를 검증하도록 만든 클라이언트다. endpoint 는 `https://…` 이고,
        # ssl_context 없이 `http://…` 를 주는 것은 로컬 테스트용 가짜 서버뿐이다.
        self._http = http
        self._endpoint = endpoint.rstrip("/")
        self._ssl_context = ssl_context
        self._token_provider = token_provider

    async def search_pods(self, namespace: str, container: str) -> list[PodInfo]:
        response = await self._get(f"/api/v1/namespaces/{quote(namespace, safe='')}/pods")
        items = response.json().get("items", [])
        pods = [info for item in items if (info := _to_pod_info(item, container)) is not None]
        return sorted(pods, key=lambda pod: pod.started_at or _EPOCH, reverse=True)

    async def find_pod(self, namespace: str, name: str, container: str) -> PodInfo | None:
        path = f"/api/v1/namespaces/{quote(namespace, safe='')}/pods/{quote(name, safe='')}"
        response = await self._get(path, allow_not_found=True)
        if response.status_code == 404:
            return None
        return _to_pod_info(response.json(), container)

    async def detect_shell(self, namespace: str, pod: str, container: str) -> Shell:
        channel = await self._open_exec(
            namespace, pod, container, _SHELL_PROBE_COMMAND, is_tty=False, has_stdin=False
        )
        output = b""
        result: ExecExit | None = None
        try:
            async with asyncio.timeout(SHELL_PROBE_TIMEOUT_SECONDS):
                async for event in channel.events():
                    if isinstance(event, ExecOutput):
                        output += event.data
                    else:
                        result = event
        except TimeoutError as exc:
            raise ClusterUnavailableError("shell probe timed out", pod=pod) from exc
        finally:
            await channel.close()
        if result is not None and result.exit_code == 0:
            return "bash" if output.decode("utf-8", "ignore").strip() == "bash" else "sh"
        if result is not None and _is_missing_executable(result):
            raise ShellNotFoundError("container has no shell", pod=pod)
        raise ClusterUnavailableError("shell probe failed", pod=pod)

    async def open_exec(
        self, namespace: str, pod: str, container: str, shell: Shell, cols: int, rows: int
    ) -> ExecChannel:
        command = ["/bin/sh", "-c", f"TERM=xterm-256color exec {shell}"]
        channel = await self._open_exec(
            namespace, pod, container, command, is_tty=True, has_stdin=True
        )
        await channel.resize(cols, rows)
        return channel

    async def _get(self, path: str, *, allow_not_found: bool = False) -> httpx.Response:
        headers = {"Authorization": f"Bearer {await self._token_provider.get_token()}"}
        try:
            response = await self._http.get(path, headers=headers)
        except httpx.HTTPError as exc:
            raise ClusterUnavailableError("cluster request failed") from exc
        if response.status_code == 404 and allow_not_found:
            return response
        if response.status_code != 200:
            # 401·403 은 토큰·RBAC 설정 문제다. 응답 본문은 메시지에 옮기지 않는다.
            raise ClusterUnavailableError("cluster rejected request", status=response.status_code)
        return response

    async def _open_exec(
        self,
        namespace: str,
        pod: str,
        container: str,
        command: list[str],
        *,
        is_tty: bool,
        has_stdin: bool,
    ) -> "_WebSocketExecChannel":
        query = [
            ("container", container),
            ("stdin", _bool(has_stdin)),
            ("stdout", "true"),
            # TTY 에서는 stderr 가 stdout 에 합쳐지므로 따로 열지 않는다.
            ("stderr", _bool(not is_tty)),
            ("tty", _bool(is_tty)),
            *(("command", part) for part in command),
        ]
        # 운영 설정은 https 만 받는다(ConsoleGatewaySettings). http 는 로컬 테스트용 가짜 서버다.
        is_tls = self._endpoint.startswith("https://")
        ws_endpoint = ("wss://" if is_tls else "ws://") + self._endpoint.split("://", 1)[1]
        uri = (
            f"{ws_endpoint}/api/v1/namespaces/{quote(namespace, safe='')}"
            f"/pods/{quote(pod, safe='')}/exec?{urlencode(query)}"
        )
        headers = {"Authorization": f"Bearer {await self._token_provider.get_token()}"}
        try:
            ws = await connect(
                uri,
                ssl=self._ssl_context if is_tls else None,
                subprotocols=[Subprotocol(EXEC_SUBPROTOCOL)],
                additional_headers=headers,
                open_timeout=CONNECT_TIMEOUT_SECONDS,
                ping_interval=CLUSTER_PING_INTERVAL_SECONDS,
                ping_timeout=None,
            )
        except InvalidStatus as exc:
            raise _map_handshake_status(exc, pod) from exc
        except (InvalidHandshake, OSError, TimeoutError) as exc:
            raise ClusterUnavailableError("cluster exec connection failed") from exc
        if ws.subprotocol != EXEC_SUBPROTOCOL:
            await ws.close()
            raise ClusterUnavailableError("cluster does not support exec protocol")
        return _WebSocketExecChannel(ws)


class _WebSocketExecChannel:
    def __init__(self, ws: ClientConnection) -> None:
        self._ws = ws

    async def send_input(self, data: bytes) -> None:
        await self._send(CHANNEL_STDIN, data)

    async def resize(self, cols: int, rows: int) -> None:
        await self._send(CHANNEL_RESIZE, json.dumps({"Width": cols, "Height": rows}).encode())

    async def _send(self, channel: int, payload: bytes) -> None:
        try:
            await self._ws.send(bytes([channel]) + payload)
        except ConnectionClosed:
            # 연결이 이미 끝났다. 끝났다는 사실은 events() 가 ExecExit 로 알린다.
            return

    async def events(self) -> AsyncIterator[ExecEvent]:
        try:
            async for message in self._ws:
                if isinstance(message, str) or len(message) < 2:
                    continue
                channel, payload = message[0], message[1:]
                if channel in (CHANNEL_STDOUT, CHANNEL_STDERR):
                    yield ExecOutput(payload)
                elif channel == CHANNEL_STATUS:
                    yield _parse_status(payload)
                    return
        except ConnectionClosed:
            pass
        # 상태 없이 연결이 끊겼다.
        yield ExecExit(exit_code=None)

    async def close(self) -> None:
        await self._ws.close()


_EPOCH = datetime.fromtimestamp(0, UTC)


def _bool(value: bool) -> str:
    return "true" if value else "false"


def _to_pod_info(item: dict[str, Any], container: str) -> PodInfo | None:
    spec = item.get("spec", {})
    if container not in {entry.get("name") for entry in spec.get("containers", [])}:
        return None
    metadata = item.get("metadata", {})
    status = item.get("status", {})
    phase = "Terminating" if metadata.get("deletionTimestamp") else status.get("phase", "Unknown")
    is_container_ready = any(
        entry.get("name") == container and entry.get("ready") is True
        for entry in status.get("containerStatuses", [])
    )
    release_label = (metadata.get("labels") or {}).get(RELEASE_LABEL)
    return PodInfo(
        name=metadata["name"],
        phase=phase,
        is_ready=phase == "Running" and is_container_ready,
        started_at=_parse_time(status.get("startTime")),
        release_id=int(release_label) if release_label and release_label.isdigit() else None,
    )


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _parse_status(payload: bytes) -> ExecExit:
    """채널 3 의 `metav1.Status` JSON 을 종료 결과로 바꾼다."""
    try:
        status = json.loads(payload)
    except ValueError:
        return ExecExit(exit_code=None, message="invalid status")
    if not isinstance(status, dict):
        return ExecExit(exit_code=None, message="invalid status")
    if status.get("status") == "Success":
        return ExecExit(exit_code=0)
    message = status.get("message") if isinstance(status.get("message"), str) else None
    if status.get("reason") == "NonZeroExitCode":
        for cause in (status.get("details") or {}).get("causes", []):
            if cause.get("reason") == "ExitCode":
                try:
                    return ExecExit(exit_code=int(cause["message"]), message=message)
                except (KeyError, TypeError, ValueError):
                    break
    return ExecExit(exit_code=None, message=message)


def _is_missing_executable(result: ExecExit) -> bool:
    if result.exit_code in _MISSING_EXECUTABLE_EXIT_CODES:
        return True
    message = (result.message or "").lower()
    return any(marker in message for marker in _MISSING_EXECUTABLE_MARKERS)


def _map_handshake_status(exc: InvalidStatus, pod: str) -> Exception:
    status_code = exc.response.status_code
    body = exc.response.body.decode("utf-8", "ignore").lower() if exc.response.body else ""
    if any(marker in body for marker in _MISSING_EXECUTABLE_MARKERS):
        return ShellNotFoundError("container has no shell", pod=pod)
    if status_code == 404:
        return PodNotFoundError("pod not found", pod=pod)
    if status_code in (400, 409):
        return PodNotReadyError("pod is not ready", pod=pod)
    return ClusterUnavailableError("cluster rejected exec", status=status_code)
