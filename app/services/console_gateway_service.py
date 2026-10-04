"""Console Gateway 의 서비스 로직 — ticket 검증, Pod 목록, WebSocket 연결 한 번의 수명(ADR 0033).

WebSocket 자체(Starlette)는 `ConsoleTransport` 로 가려 두고, 클러스터는 `KubernetesClient` 로
가린다(AWS 는 클러스터 API, on-prem 은 Argo CD 터미널, ADR 0035). 이 모듈은 둘 다 모른 채
프로토콜(auth → ready → input/output → exit·error)과 시간·동시 연결 제한만 다룬다.
입출력 내용은 로그에 남기지 않는다.
"""

import asyncio
import codecs
import logging
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from app.clients.kubernetes_client import (
    ExecChannel,
    ExecOutput,
    KubernetesClient,
    PodInfo,
)
from app.core.console_ticket import ConsoleTicketClaims, ConsoleTicketVerifier
from app.core.exceptions import (
    AppError,
    ClusterUnavailableError,
    ConsoleSessionLimitExceededError,
    ConsoleTokenReusedError,
    PodNotFoundError,
    PodNotReadyError,
    UnauthorizedError,
)
from app.core.logging import log_context
from app.enums import ConsoleErrorCode
from app.schemas.console import (
    AuthFrame,
    ConsoleClientFrame,
    ConsoleServerFrame,
    ErrorFrame,
    ExitFrame,
    InputFrame,
    OutputFrame,
    PingFrame,
    PongFrame,
    ReadyFrame,
    ResizeFrame,
)

logger = logging.getLogger(__name__)

# 콘솔이 붙는 컨테이너. 요청에서 받지 않는다.
APP_CONTAINER = "app"
# 연결한 뒤 첫 프레임(auth)을 기다리는 시간.
AUTH_TIMEOUT_SECONDS = 5.0
# 시간 제한을 확인하는 최대 간격(초).
WATCH_INTERVAL_SECONDS = 1.0
# 연결에 쓴 ticket 을 만료 뒤에도 기억하는 여유. 시계 오차(검증의 leeway)보다 길어야 한다.
USED_TICKET_GRACE = timedelta(seconds=30)

ErrorMessages = Mapping[ConsoleErrorCode, str]
CONSOLE_ERROR_MESSAGES: ErrorMessages = {
    ConsoleErrorCode.UNAUTHORIZED: "인증에 실패했어요",
    ConsoleErrorCode.TOKEN_EXPIRED: "연결 시간이 지났어요. 다시 연결해 주세요",
    ConsoleErrorCode.TOKEN_REUSED: "이미 사용한 연결 정보예요. 다시 연결해 주세요",
    ConsoleErrorCode.POD_NOT_FOUND: "레플리카를 찾을 수 없어요",
    ConsoleErrorCode.POD_NOT_READY: "레플리카가 아직 준비되지 않았어요",
    ConsoleErrorCode.SHELL_NOT_FOUND: "이 이미지에는 셸이 없어요",
    ConsoleErrorCode.SESSION_LIMIT_EXCEEDED: "동시에 열 수 있는 콘솔 수를 넘었어요",
    ConsoleErrorCode.IDLE_TIMEOUT: "입력이 없어 연결을 종료했어요",
    ConsoleErrorCode.MAX_DURATION_EXCEEDED: "최대 연결 시간이 지나 연결을 종료했어요",
    ConsoleErrorCode.CLUSTER_UNAVAILABLE: "클러스터에 연결하지 못했어요",
    ConsoleErrorCode.INTERNAL_ERROR: "콘솔에서 오류가 발생했어요",
}

EndReason = Literal["client_closed", "shell_exited", "idle_timeout", "max_duration", "error"]


class TransportClosedError(Exception):
    """클라이언트 쪽 연결이 이미 끊겨 프레임을 보낼 수 없다."""


class ConsoleTransport(Protocol):
    """Gateway 가 보는 WebSocket. 해석할 수 없는 프레임은 구현이 건너뛴다."""

    async def receive(self) -> ConsoleClientFrame | None:
        """다음 클라이언트 프레임. 연결이 끝났으면 None."""
        ...

    async def send(self, frame: ConsoleServerFrame) -> None:
        """프레임을 보낸다. 연결이 끊겼으면 TransportClosedError."""
        ...


@dataclass(frozen=True)
class ConsoleLimits:
    idle_timeout_seconds: float
    max_session_seconds: float
    max_sessions_per_user: int


class ConsoleSessionRegistry:
    """메모리 기반 상태 — 연결에 쓴 ticket(jti)과 사용자별 동시 연결 수.

    replica 하나에서만 정확하다(ADR 0033). 재시작하면 사라지지만 ticket 수명이 60초라 그 안에서만
    다시 쓸 수 있다.
    """

    def __init__(
        self,
        max_sessions_per_user: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._max_sessions_per_user = max_sessions_per_user
        self._clock = clock
        self._used_expires_at: dict[str, datetime] = {}
        self._active_by_user_id: dict[int, int] = {}

    def consume_ticket(self, session_id: str, expires_at: datetime) -> None:
        """ticket 을 연결에 쓴 것으로 기록한다. 이미 썼으면 ConsoleTokenReusedError."""
        now = self._clock()
        for used_id, used_expires_at in list(self._used_expires_at.items()):
            if used_expires_at + USED_TICKET_GRACE < now:
                del self._used_expires_at[used_id]
        if session_id in self._used_expires_at:
            raise ConsoleTokenReusedError("console ticket already used")
        self._used_expires_at[session_id] = expires_at

    @contextmanager
    def hold_slot(self, user_id: int) -> Iterator[None]:
        """동시 연결 한 칸을 잡는다. 한도를 넘으면 ConsoleSessionLimitExceededError."""
        active = self._active_by_user_id.get(user_id, 0)
        if active >= self._max_sessions_per_user:
            raise ConsoleSessionLimitExceededError(
                "console session limit exceeded", limit=self._max_sessions_per_user
            )
        self._active_by_user_id[user_id] = active + 1
        try:
            yield
        finally:
            remaining = self._active_by_user_id[user_id] - 1
            if remaining:
                self._active_by_user_id[user_id] = remaining
            else:
                del self._active_by_user_id[user_id]


@dataclass(frozen=True)
class _Authenticated:
    claims: ConsoleTicketClaims
    cols: int
    rows: int


class _Activity:
    """사용자의 마지막 입력 시각(monotonic). ping·pong 은 입력이 아니다."""

    def __init__(self) -> None:
        self.last_input_at = time.monotonic()


class ConsoleGatewayService:
    def __init__(
        self,
        verifier: ConsoleTicketVerifier,
        clusters: Mapping[str, KubernetesClient],
        registry: ConsoleSessionRegistry,
        limits: ConsoleLimits,
    ) -> None:
        self._verifier = verifier
        self._clusters = clusters
        self._registry = registry
        self._limits = limits

    async def search_pods(self, token: str) -> list[PodInfo]:
        claims = self._verifier.verify(token)
        return await self._get_cluster(claims).search_pods(claims.namespace, APP_CONTAINER)

    async def serve_exec(self, transport: ConsoleTransport, pod_name: str) -> None:
        """WebSocket 연결 한 번을 끝까지 처리한다. 실패는 `error` 프레임으로 알리고 반환한다."""
        started_at = time.monotonic()
        try:
            auth = await self._authenticate(transport)
            with (
                log_context(
                    session_id=auth.claims.session_id,
                    user_id=auth.claims.user_id,
                    service_id=auth.claims.service_id,
                ),
                self._registry.hold_slot(auth.claims.user_id),
            ):
                await self._run_exec(transport, auth, pod_name, started_at)
        except TransportClosedError:
            # 클라이언트가 먼저 떠났다. 알릴 곳이 없다.
            return
        except AppError as exc:
            code = _to_error_code(exc)
            logger.info(
                "console session rejected",
                extra={"action": "serve_exec", "error_code": code, **exc.fields},
            )
            await self._send_error(transport, code)
        except Exception:
            # 경계에서 한 번만 기록한다. 화면에는 내부 사정을 알리지 않는다.
            logger.exception("console session crashed", extra={"action": "serve_exec"})
            await self._send_error(transport, ConsoleErrorCode.INTERNAL_ERROR)

    async def _authenticate(self, transport: ConsoleTransport) -> _Authenticated:
        try:
            async with asyncio.timeout(AUTH_TIMEOUT_SECONDS):
                frame = await transport.receive()
        except TimeoutError as exc:
            raise UnauthorizedError("auth frame timed out") from exc
        if not isinstance(frame, AuthFrame):
            raise UnauthorizedError("auth frame required")
        claims = self._verifier.verify(frame.token)
        self._registry.consume_ticket(claims.session_id, claims.expires_at)
        return _Authenticated(claims, frame.cols, frame.rows)

    async def _run_exec(
        self, transport: ConsoleTransport, auth: _Authenticated, pod_name: str, started_at: float
    ) -> None:
        claims = auth.claims
        cluster = self._get_cluster(claims)
        pod = await cluster.find_pod(claims.namespace, pod_name, APP_CONTAINER)
        if pod is None:
            raise PodNotFoundError("pod not found", pod=pod_name)
        if not pod.is_ready:
            raise PodNotReadyError("pod is not ready", pod=pod_name)
        shell = await cluster.detect_shell(claims.namespace, pod_name, APP_CONTAINER)
        channel = await cluster.open_exec(
            claims.namespace, pod_name, APP_CONTAINER, shell, auth.cols, auth.rows
        )
        log_fields = {"pod": pod_name, "target_id": claims.target_id, "cluster": claims.cluster}
        logger.info(
            "console session started", extra={"action": "console_session_started", **log_fields}
        )
        end_reason: EndReason = "error"
        try:
            await transport.send(ReadyFrame(pod=pod_name, shell=shell))
            end_reason = await self._relay(transport, channel)
        finally:
            await channel.close()
            logger.info(
                "console session ended",
                extra={
                    "action": "console_session_ended",
                    "end_reason": end_reason,
                    "duration_seconds": round(time.monotonic() - started_at, 1),
                    **log_fields,
                },
            )

    async def _relay(self, transport: ConsoleTransport, channel: ExecChannel) -> EndReason:
        """입력·출력·시간 제한 세 작업 중 먼저 끝나는 쪽이 연결의 끝을 정한다."""
        activity = _Activity()
        tasks = (
            asyncio.create_task(self._pump_input(transport, channel, activity)),
            asyncio.create_task(self._pump_output(transport, channel)),
            asyncio.create_task(self._watch(activity)),
        )
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        result = next(iter(done)).result()
        if isinstance(result, ConsoleErrorCode):
            await self._send_error(transport, result)
            return "idle_timeout" if result == ConsoleErrorCode.IDLE_TIMEOUT else "max_duration"
        return result

    async def _pump_input(
        self, transport: ConsoleTransport, channel: ExecChannel, activity: _Activity
    ) -> EndReason:
        try:
            while True:
                frame = await transport.receive()
                if frame is None:
                    return "client_closed"
                if isinstance(frame, InputFrame):
                    activity.last_input_at = time.monotonic()
                    await channel.send_input(frame.data.encode())
                elif isinstance(frame, ResizeFrame):
                    await channel.resize(frame.cols, frame.rows)
                elif isinstance(frame, PingFrame):
                    await transport.send(PongFrame())
        except TransportClosedError:
            return "client_closed"

    async def _pump_output(self, transport: ConsoleTransport, channel: ExecChannel) -> EndReason:
        # 한 문자가 두 프레임에 걸쳐 올 수 있어 증분 디코더로 푼다. 깨진 바이트는 대체 문자다.
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            async for event in channel.events():
                if isinstance(event, ExecOutput):
                    text = decoder.decode(event.data)
                    if text:
                        await transport.send(OutputFrame(data=text))
                    continue
                tail = decoder.decode(b"", final=True)
                if tail:
                    await transport.send(OutputFrame(data=tail))
                await transport.send(ExitFrame(code=event.exit_code))
                break
        except TransportClosedError:
            return "client_closed"
        return "shell_exited"

    async def _watch(self, activity: _Activity) -> ConsoleErrorCode:
        started_at = time.monotonic()
        while True:
            now = time.monotonic()
            max_left = self._limits.max_session_seconds - (now - started_at)
            idle_left = self._limits.idle_timeout_seconds - (now - activity.last_input_at)
            if max_left <= 0:
                return ConsoleErrorCode.MAX_DURATION_EXCEEDED
            if idle_left <= 0:
                return ConsoleErrorCode.IDLE_TIMEOUT
            await asyncio.sleep(min(max_left, idle_left, WATCH_INTERVAL_SECONDS))

    def _get_cluster(self, claims: ConsoleTicketClaims) -> KubernetesClient:
        cluster = self._clusters.get(claims.cluster)
        if cluster is None:
            raise ClusterUnavailableError("cluster is not supported", cluster=claims.cluster)
        return cluster

    @staticmethod
    async def _send_error(transport: ConsoleTransport, code: ConsoleErrorCode) -> None:
        """error 프레임을 보낸다. 상대가 이미 끊겼으면 보낼 곳이 없어 조용히 넘어간다."""
        try:
            await transport.send(ErrorFrame(code=code, message=CONSOLE_ERROR_MESSAGES[code]))
        except TransportClosedError:
            return


def _to_error_code(exc: AppError) -> ConsoleErrorCode:
    try:
        return ConsoleErrorCode(exc.code)
    except ValueError:
        return ConsoleErrorCode.INTERNAL_ERROR
