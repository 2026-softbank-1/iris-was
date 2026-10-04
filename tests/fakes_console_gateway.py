"""Console Gateway 테스트용 가짜 클러스터·가짜 WebSocket."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from app.clients.kubernetes_client import (
    ExecEvent,
    ExecExit,
    ExecOutput,
    PodInfo,
    Shell,
)
from app.core.console_ticket import (
    CONSOLE_CLUSTER_AWS,
    ConsoleTicketSigner,
    ConsoleTicketVerifier,
)
from app.core.exceptions import ShellNotFoundError
from app.schemas.console import ConsoleClientFrame, ConsoleServerFrame
from app.services.console_gateway_service import (
    ConsoleGatewayService,
    ConsoleLimits,
    ConsoleSessionRegistry,
    TransportClosedError,
)
from tests.fakes_console import generate_ed25519_pem_pair

WAIT_TIMEOUT_SECONDS = 2.0
POD = "app-6d9f7c-abcde"
SERVICE_ID = 42


class FakeExecChannel:
    """셸 대신 입력을 되돌려 주는 채널. `exit\\r` 을 받으면 종료 코드 3 으로 끝난다."""

    def __init__(self, *, echoes_input: bool = True) -> None:
        self.inputs: list[bytes] = []
        self.resizes: list[tuple[int, int]] = []
        self.is_closed = False
        self.closed = asyncio.Event()
        self.echoes_input = echoes_input
        self._events: asyncio.Queue[ExecEvent] = asyncio.Queue()

    def push(self, event: ExecEvent) -> None:
        self._events.put_nowait(event)

    async def send_input(self, data: bytes) -> None:
        self.inputs.append(data)
        if self.echoes_input:
            self.push(ExecOutput(data))
        if data == b"exit\r":
            self.push(ExecExit(exit_code=3))

    async def resize(self, cols: int, rows: int) -> None:
        self.resizes.append((cols, rows))

    async def events(self) -> AsyncIterator[ExecEvent]:
        while True:
            event = await self._events.get()
            yield event
            if isinstance(event, ExecExit):
                return

    async def close(self) -> None:
        self.is_closed = True
        self.closed.set()


def make_pod(
    name: str = "app-6d9f7c-abcde",
    *,
    is_ready: bool = True,
    phase: str = "Running",
    release_id: int | None = 123,
    started_at: datetime | None = None,
) -> PodInfo:
    return PodInfo(
        name=name,
        phase=phase,
        is_ready=is_ready,
        started_at=started_at or datetime(2026, 10, 4, 11, 0, tzinfo=UTC),
        release_id=release_id,
    )


class FakeKubernetes:
    """`KubernetesClient` 를 흉내 낸다. 호출 인자를 기록해 namespace·컨테이너 고정을 검증한다."""

    def __init__(self, pods: list[PodInfo] | None = None) -> None:
        self.pods = pods if pods is not None else [make_pod()]
        self.shell: Shell = "bash"
        self.shell_error: Exception | None = None
        self.channel = FakeExecChannel()
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def search_pods(self, namespace: str, container: str) -> list[PodInfo]:
        self.calls.append(("search_pods", (namespace, container)))
        return list(self.pods)

    async def find_pod(self, namespace: str, name: str, container: str) -> PodInfo | None:
        self.calls.append(("find_pod", (namespace, name, container)))
        return next((pod for pod in self.pods if pod.name == name), None)

    async def detect_shell(self, namespace: str, pod: str, container: str) -> Shell:
        self.calls.append(("detect_shell", (namespace, pod, container)))
        if self.shell_error is not None:
            raise self.shell_error
        return self.shell

    async def open_exec(
        self, namespace: str, pod: str, container: str, shell: Shell, cols: int, rows: int
    ) -> FakeExecChannel:
        self.calls.append(("open_exec", (namespace, pod, container, shell, cols, rows)))
        return self.channel


def shell_missing() -> Exception:
    return ShellNotFoundError("container has no shell")


class FakeTransport:
    """클라이언트 프레임은 `feed` 로 넣고, 서버가 보낸 프레임은 `sent` 에서 읽는다."""

    def __init__(self) -> None:
        self.sent: list[ConsoleServerFrame] = []
        self.is_closed = False
        self._incoming: asyncio.Queue[ConsoleClientFrame | None] = asyncio.Queue()
        self._sent_event = asyncio.Event()

    def feed(self, frame: ConsoleClientFrame | None) -> None:
        self._incoming.put_nowait(frame)

    async def receive(self) -> ConsoleClientFrame | None:
        return await self._incoming.get()

    async def send(self, frame: ConsoleServerFrame) -> None:
        if self.is_closed:
            raise TransportClosedError
        self.sent.append(frame)
        self._sent_event.set()

    def close_from_client(self) -> None:
        """클라이언트가 연결을 끊었다: 읽기는 None, 이후 쓰기는 실패한다."""
        self.is_closed = True
        self.feed(None)

    async def wait_for_frame(self, frame_type: str) -> ConsoleServerFrame:
        """아직 보지 않은 프레임까지 포함해 type 이 맞는 첫 프레임을 기다린다."""
        async with asyncio.timeout(WAIT_TIMEOUT_SECONDS):
            while True:
                for frame in self.sent:
                    if frame.type == frame_type:
                        return frame
                self._sent_event.clear()
                await self._sent_event.wait()


class Harness:
    def __init__(
        self,
        *,
        idle_timeout: float = 900,
        max_duration: float = 3600,
        max_sessions: int = 3,
        cluster: FakeKubernetes | None = None,
    ) -> None:
        private_pem, public_pem = generate_ed25519_pem_pair()
        self.signer = ConsoleTicketSigner(private_pem)
        self.cluster = cluster or FakeKubernetes()
        self.service = ConsoleGatewayService(
            ConsoleTicketVerifier(public_pem),
            {CONSOLE_CLUSTER_AWS: self.cluster},
            ConsoleSessionRegistry(max_sessions),
            ConsoleLimits(idle_timeout, max_duration, max_sessions),
        )

    def ticket(
        self,
        *,
        user_id: int = 7,
        service_id: int = SERVICE_ID,
        session_id: str = "session-1",
        cluster: str = CONSOLE_CLUSTER_AWS,
        now: datetime | None = None,
    ) -> str:
        token, _ = self.signer.sign(session_id, user_id, service_id, 1, cluster, now)
        return token

    def start(self, transport: FakeTransport, pod: str = POD) -> "asyncio.Task[None]":
        return asyncio.create_task(self.service.serve_exec(transport, pod))
