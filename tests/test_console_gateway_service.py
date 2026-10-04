import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.clients.kubernetes_client import ExecExit, ExecOutput
from app.core.exceptions import (
    ClusterUnavailableError,
    ConsoleSessionLimitExceededError,
    ConsoleTokenExpiredError,
    ConsoleTokenReusedError,
    UnauthorizedError,
)
from app.enums import ConsoleErrorCode
from app.schemas.console import (
    CONSOLE_CLIENT_FRAME_ADAPTER,
    AuthFrame,
    ErrorFrame,
    ExitFrame,
    InputFrame,
    OutputFrame,
    PingFrame,
    PongFrame,
    ReadyFrame,
    ResizeFrame,
)
from app.services import console_gateway_service
from app.services.console_gateway_service import (
    ConsoleSessionRegistry,
)
from tests.fakes_console_gateway import (
    POD,
    FakeKubernetes,
    FakeTransport,
    Harness,
    make_pod,
    shell_missing,
)


async def open_session(
    harness: Harness, transport: FakeTransport, token: str, *, cols: int = 80, rows: int = 24
) -> "asyncio.Task[None]":
    task = harness.start(transport)
    transport.feed(AuthFrame(token=token, cols=cols, rows=rows))
    await transport.wait_for_frame("ready")
    return task


def sent_types(transport: FakeTransport) -> list[str]:
    return [frame.type for frame in transport.sent]


async def finish(task: "asyncio.Task[None]") -> None:
    async with asyncio.timeout(2):
        await task


# ---- 정상 흐름 ------------------------------------------------------------------------------


async def test_serve_exec_relays_input_output_resize_ping_and_exit() -> None:
    harness = Harness()
    transport = FakeTransport()

    task = await open_session(harness, transport, harness.ticket(), cols=100, rows=30)
    transport.feed(InputFrame(data="ls\r"))
    transport.feed(ResizeFrame(cols=120, rows=40))
    transport.feed(PingFrame())
    await transport.wait_for_frame("pong")
    await transport.wait_for_frame("output")
    transport.feed(InputFrame(data="exit\r"))
    await finish(task)

    assert transport.sent[0] == ReadyFrame(pod=POD, shell="bash")
    assert OutputFrame(data="ls\r") in transport.sent
    assert PongFrame() in transport.sent
    assert transport.sent[-1] == ExitFrame(code=3)
    assert ErrorFrame not in {type(frame) for frame in transport.sent}
    assert harness.cluster.channel.inputs == [b"ls\r", b"exit\r"]
    assert harness.cluster.channel.resizes == [(120, 40)]
    assert harness.cluster.channel.is_closed


async def test_serve_exec_uses_namespace_and_container_from_ticket_only() -> None:
    harness = Harness()
    transport = FakeTransport()

    task = await open_session(harness, transport, harness.ticket(service_id=9), cols=100, rows=30)
    transport.close_from_client()
    await finish(task)

    assert harness.cluster.calls == [
        ("find_pod", ("svc-9", POD, "app")),
        ("detect_shell", ("svc-9", POD, "app")),
        ("open_exec", ("svc-9", POD, "app", "bash", 100, 30)),
    ]


async def test_serve_exec_reports_sh_when_bash_is_missing() -> None:
    harness = Harness()
    harness.cluster.shell = "sh"
    transport = FakeTransport()

    task = await open_session(harness, transport, harness.ticket())
    transport.close_from_client()
    await finish(task)

    assert transport.sent[0] == ReadyFrame(pod=POD, shell="sh")


async def test_serve_exec_client_disconnect_closes_channel_without_error_frame() -> None:
    harness = Harness()
    transport = FakeTransport()

    task = await open_session(harness, transport, harness.ticket())
    transport.close_from_client()
    await finish(task)

    assert sent_types(transport) == ["ready"]
    assert harness.cluster.channel.is_closed


async def test_serve_exec_decodes_utf8_split_across_frames() -> None:
    harness = Harness()
    harness.cluster.channel.echoes_input = False
    transport = FakeTransport()
    task = await open_session(harness, transport, harness.ticket())
    channel = harness.cluster.channel

    channel.push(ExecOutput("안".encode()[:2]))
    channel.push(ExecOutput("안".encode()[2:] + b"\xff"))
    channel.push(ExecExit(exit_code=None))
    await finish(task)

    outputs = [frame.data for frame in transport.sent if isinstance(frame, OutputFrame)]
    assert "".join(outputs) == "안�"
    assert transport.sent[-1] == ExitFrame(code=None)


async def test_exit_frame_without_code_omits_code_from_json() -> None:
    assert ExitFrame(code=None).model_dump_json(exclude_none=True) == '{"type":"exit"}'
    assert json.loads(ExitFrame(code=0).model_dump_json(exclude_none=True)) == {
        "type": "exit",
        "code": 0,
    }


# ---- 인증·ticket -----------------------------------------------------------------------------


async def _first_error(transport: FakeTransport, task: "asyncio.Task[None]") -> ErrorFrame:
    await finish(task)
    errors = [frame for frame in transport.sent if isinstance(frame, ErrorFrame)]
    assert len(errors) == 1
    assert transport.sent[-1] is errors[0]
    return errors[0]


async def test_serve_exec_invalid_token_sends_unauthorized() -> None:
    harness = Harness()
    transport = FakeTransport()
    task = harness.start(transport)

    transport.feed(AuthFrame(token="garbage"))
    error = await _first_error(transport, task)

    assert error.code == ConsoleErrorCode.UNAUTHORIZED
    assert harness.cluster.calls == []


async def test_serve_exec_expired_token_sends_token_expired() -> None:
    harness = Harness()
    transport = FakeTransport()
    task = harness.start(transport)

    transport.feed(AuthFrame(token=harness.ticket(now=datetime.now(UTC) - timedelta(minutes=5))))
    error = await _first_error(transport, task)

    assert error.code == ConsoleErrorCode.TOKEN_EXPIRED


async def test_serve_exec_first_frame_not_auth_sends_unauthorized() -> None:
    harness = Harness()
    transport = FakeTransport()
    task = harness.start(transport)

    transport.feed(InputFrame(data="ls\r"))
    error = await _first_error(transport, task)

    assert error.code == ConsoleErrorCode.UNAUTHORIZED


async def test_serve_exec_without_auth_frame_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(console_gateway_service, "AUTH_TIMEOUT_SECONDS", 0.05)
    harness = Harness()
    transport = FakeTransport()

    task = harness.start(transport)
    error = await _first_error(transport, task)

    assert error.code == ConsoleErrorCode.UNAUTHORIZED


async def test_serve_exec_client_leaves_before_auth_sends_nothing() -> None:
    harness = Harness()
    transport = FakeTransport()
    task = harness.start(transport)

    transport.close_from_client()
    await finish(task)

    assert transport.sent == []


async def test_serve_exec_reused_ticket_sends_token_reused() -> None:
    harness = Harness()
    token = harness.ticket()
    first = FakeTransport()
    task = await open_session(harness, first, token)
    first.close_from_client()
    await finish(task)

    second = FakeTransport()
    second_task = harness.start(second)
    second.feed(AuthFrame(token=token))
    error = await _first_error(second, second_task)

    assert error.code == ConsoleErrorCode.TOKEN_REUSED


async def test_serve_exec_unsupported_cluster_sends_cluster_unavailable() -> None:
    harness = Harness()
    transport = FakeTransport()
    task = harness.start(transport)

    transport.feed(AuthFrame(token=harness.ticket(cluster="onprem-abcd1234")))
    error = await _first_error(transport, task)

    assert error.code == ConsoleErrorCode.CLUSTER_UNAVAILABLE


# ---- Pod·셸 ----------------------------------------------------------------------------------


async def test_serve_exec_unknown_pod_sends_pod_not_found() -> None:
    harness = Harness()
    transport = FakeTransport()
    task = harness.start(transport, "no-such-pod")

    transport.feed(AuthFrame(token=harness.ticket()))
    error = await _first_error(transport, task)

    assert error.code == ConsoleErrorCode.POD_NOT_FOUND


async def test_serve_exec_not_ready_pod_sends_pod_not_ready() -> None:
    harness = Harness(cluster=FakeKubernetes([make_pod(is_ready=False, phase="Pending")]))
    transport = FakeTransport()
    task = harness.start(transport)

    transport.feed(AuthFrame(token=harness.ticket()))
    error = await _first_error(transport, task)

    assert error.code == ConsoleErrorCode.POD_NOT_READY
    assert [name for name, _ in harness.cluster.calls] == ["find_pod"]


async def test_serve_exec_shell_missing_sends_shell_not_found() -> None:
    harness = Harness()
    harness.cluster.shell_error = shell_missing()
    transport = FakeTransport()
    task = harness.start(transport)

    transport.feed(AuthFrame(token=harness.ticket()))
    error = await _first_error(transport, task)

    assert error.code == ConsoleErrorCode.SHELL_NOT_FOUND
    assert error.message == "이 이미지에는 셸이 없어요"
    assert "open_exec" not in [name for name, _ in harness.cluster.calls]


async def test_serve_exec_unexpected_failure_sends_internal_error() -> None:
    harness = Harness()
    harness.cluster.shell_error = RuntimeError("boom with secret detail")
    transport = FakeTransport()
    task = harness.start(transport)

    transport.feed(AuthFrame(token=harness.ticket()))
    error = await _first_error(transport, task)

    assert error.code == ConsoleErrorCode.INTERNAL_ERROR
    assert "secret" not in error.message


# ---- 동시 연결 한도 ---------------------------------------------------------------------------


async def test_serve_exec_session_limit_per_user() -> None:
    harness = Harness(max_sessions=1)
    first = FakeTransport()
    first_task = await open_session(harness, first, harness.ticket(session_id="a"))

    blocked = FakeTransport()
    blocked_task = harness.start(blocked)
    blocked.feed(AuthFrame(token=harness.ticket(session_id="b")))
    error = await _first_error(blocked, blocked_task)

    other_user = FakeTransport()
    other_user_task = await open_session(
        harness, other_user, harness.ticket(user_id=8, session_id="c")
    )
    other_user.close_from_client()
    await finish(other_user_task)

    first.close_from_client()
    await finish(first_task)
    again = FakeTransport()
    again_task = await open_session(harness, again, harness.ticket(session_id="d"))
    again.close_from_client()
    await finish(again_task)

    assert error.code == ConsoleErrorCode.SESSION_LIMIT_EXCEEDED


async def test_serve_exec_releases_slot_when_open_fails() -> None:
    harness = Harness(max_sessions=1)
    harness.cluster.shell_error = shell_missing()
    for index in range(3):
        transport = FakeTransport()
        task = harness.start(transport)
        transport.feed(AuthFrame(token=harness.ticket(session_id=f"s{index}")))
        error = await _first_error(transport, task)
        assert error.code == ConsoleErrorCode.SHELL_NOT_FOUND


# ---- 시간 제한 -------------------------------------------------------------------------------


async def test_serve_exec_idle_timeout_ends_session() -> None:
    harness = Harness(idle_timeout=0.1)
    transport = FakeTransport()

    task = await open_session(harness, transport, harness.ticket())
    error = await _first_error(transport, task)

    assert error.code == ConsoleErrorCode.IDLE_TIMEOUT
    assert harness.cluster.channel.is_closed


async def test_serve_exec_ping_does_not_reset_idle_timer() -> None:
    harness = Harness(idle_timeout=0.15)
    transport = FakeTransport()
    task = await open_session(harness, transport, harness.ticket())

    async def keep_pinging() -> None:
        while True:
            transport.feed(PingFrame())
            await asyncio.sleep(0.02)

    pinger = asyncio.create_task(keep_pinging())
    try:
        error = await _first_error(transport, task)
    finally:
        pinger.cancel()

    assert error.code == ConsoleErrorCode.IDLE_TIMEOUT


async def test_serve_exec_input_resets_idle_timer() -> None:
    harness = Harness(idle_timeout=0.15)
    transport = FakeTransport()
    task = await open_session(harness, transport, harness.ticket())

    for _ in range(5):
        await asyncio.sleep(0.06)
        transport.feed(InputFrame(data="x"))
    assert not task.done()
    error = await _first_error(transport, task)

    assert error.code == ConsoleErrorCode.IDLE_TIMEOUT
    assert harness.cluster.channel.inputs == [b"x"] * 5


async def test_serve_exec_max_duration_ends_session_even_with_input() -> None:
    harness = Harness(max_duration=0.15)
    transport = FakeTransport()
    task = await open_session(harness, transport, harness.ticket())

    async def keep_typing() -> None:
        while True:
            transport.feed(InputFrame(data="x"))
            await asyncio.sleep(0.02)

    typer = asyncio.create_task(keep_typing())
    try:
        error = await _first_error(transport, task)
    finally:
        typer.cancel()

    assert error.code == ConsoleErrorCode.MAX_DURATION_EXCEEDED


# ---- search_pods ----------------------------------------------------------------------------


async def test_search_pods_lists_pods_of_ticket_namespace() -> None:
    harness = Harness()

    pods = await harness.service.search_pods(harness.ticket(service_id=9))

    assert [pod.name for pod in pods] == [POD]
    assert harness.cluster.calls == [("search_pods", ("svc-9", "app"))]


async def test_search_pods_rejects_bad_expired_and_unsupported_tickets() -> None:
    harness = Harness()

    with pytest.raises(UnauthorizedError):
        await harness.service.search_pods("garbage")
    with pytest.raises(ConsoleTokenExpiredError):
        await harness.service.search_pods(
            harness.ticket(now=datetime.now(UTC) - timedelta(minutes=5))
        )
    with pytest.raises(ClusterUnavailableError):
        await harness.service.search_pods(harness.ticket(cluster="elsewhere"))


async def test_search_pods_does_not_consume_ticket() -> None:
    harness = Harness()
    token = harness.ticket()

    await harness.service.search_pods(token)
    await harness.service.search_pods(token)

    transport = FakeTransport()
    task = await open_session(harness, transport, token)
    transport.close_from_client()
    await finish(task)


# ---- 레지스트리 ------------------------------------------------------------------------------


def test_registry_consume_ticket_rejects_reuse_and_forgets_after_grace() -> None:
    now = [datetime(2026, 10, 4, 12, 0, tzinfo=UTC)]
    registry = ConsoleSessionRegistry(3, lambda: now[0])
    expires_at = now[0] + timedelta(seconds=60)

    registry.consume_ticket("a", expires_at)
    with pytest.raises(ConsoleTokenReusedError):
        registry.consume_ticket("a", expires_at)

    now[0] += timedelta(minutes=5)
    registry.consume_ticket("b", now[0] + timedelta(seconds=60))
    # a 는 만료+여유가 지나 잊혔다(그때는 ticket 자체가 만료라 어차피 검증에서 걸린다).
    registry.consume_ticket("a", expires_at)


def test_registry_hold_slot_limits_and_releases_on_error() -> None:
    registry = ConsoleSessionRegistry(2)

    with registry.hold_slot(1), registry.hold_slot(1):
        with pytest.raises(ConsoleSessionLimitExceededError), registry.hold_slot(1):
            pass
        with registry.hold_slot(2):
            pass

    with pytest.raises(RuntimeError), registry.hold_slot(1):
        raise RuntimeError("boom")
    with registry.hold_slot(1), registry.hold_slot(1):
        pass


# ---- 프레임 스키마 ---------------------------------------------------------------------------


def test_client_frames_are_parsed_by_type() -> None:
    parse = CONSOLE_CLIENT_FRAME_ADAPTER.validate_json

    assert parse('{"type":"auth","token":"t"}') == AuthFrame(token="t", cols=80, rows=24)
    assert parse('{"type":"auth","token":"t","cols":120,"rows":30,"extra":1}').cols == 120
    assert parse('{"type":"input","data":"ls\\r"}') == InputFrame(data="ls\r")
    assert parse('{"type":"resize","cols":100,"rows":40}') == ResizeFrame(cols=100, rows=40)
    assert parse('{"type":"ping"}') == PingFrame()


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        '{"type":"unknown"}',
        '{"type":"auth"}',
        '{"type":"auth","token":""}',
        '{"type":"input","data":5}',
        '{"type":"resize","cols":0,"rows":10}',
        '{"type":"resize","cols":10,"rows":100000}',
        json.dumps({"type": "input", "data": "x" * (64 * 1024 + 1)}),
    ],
)
def test_invalid_client_frames_are_rejected(payload: str) -> None:
    with pytest.raises(ValidationError):
        CONSOLE_CLIENT_FRAME_ADAPTER.validate_json(payload)


def test_error_codes_match_exception_codes() -> None:
    """Gateway 가 던지는 도메인 예외의 code 는 모두 ConsoleErrorCode 의 값이다."""
    from app.core import exceptions

    classes = [
        exceptions.ConsoleTokenExpiredError,
        exceptions.ConsoleTokenReusedError,
        exceptions.PodNotFoundError,
        exceptions.PodNotReadyError,
        exceptions.ShellNotFoundError,
        exceptions.ConsoleSessionLimitExceededError,
        exceptions.ClusterUnavailableError,
        exceptions.UnauthorizedError,
    ]
    assert {cls.code for cls in classes} <= {code.value for code in ConsoleErrorCode}
