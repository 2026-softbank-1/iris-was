"""WebSocketTransport 가 Starlette WebSocket 의 예외·프레임 형태를 서비스가 보는 모양으로 바꾼다."""

from typing import Any

import pytest
from starlette.websockets import WebSocketDisconnect

from app.console_gateway.transport import WebSocketTransport
from app.schemas.console import AuthFrame, InputFrame, OutputFrame, PingFrame
from app.services.console_gateway_service import TransportClosedError


class FakeWebSocket:
    """Starlette `WebSocket` 중 transport 가 쓰는 부분만 흉내 낸다."""

    def __init__(self, incoming: list[dict[str, Any] | Exception] | None = None) -> None:
        self.incoming = list(incoming or [])
        self.sent: list[str] = []
        self.send_error: Exception | None = None
        self.close_error: Exception | None = None
        self.close_calls = 0

    async def receive(self) -> dict[str, Any]:
        item = self.incoming.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def send_text(self, data: str) -> None:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(data)

    async def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


def _transport(websocket: FakeWebSocket) -> WebSocketTransport:
    return WebSocketTransport(websocket)  # type: ignore[arg-type]


def _text(payload: str) -> dict[str, Any]:
    return {"type": "websocket.receive", "text": payload}


# ---- receive --------------------------------------------------------------------------------


async def test_receive_parses_text_frames() -> None:
    websocket = FakeWebSocket(
        [
            _text('{"type":"auth","token":"t"}'),
            _text('{"type":"input","data":"x"}'),
            _text('{"type":"ping"}'),
        ]
    )
    transport = _transport(websocket)

    assert await transport.receive() == AuthFrame(token="t")
    assert await transport.receive() == InputFrame(data="x")
    assert await transport.receive() == PingFrame()


async def test_receive_skips_binary_and_malformed_frames() -> None:
    websocket = FakeWebSocket(
        [
            {"type": "websocket.receive", "bytes": b"\x00"},
            _text("garbage"),
            _text('{"type":"input","data":5}'),
            _text('{"type":"ping"}'),
        ]
    )

    assert await _transport(websocket).receive() == PingFrame()


@pytest.mark.parametrize(
    "ending",
    [
        {"type": "websocket.disconnect", "code": 1000},
        WebSocketDisconnect(1006),
        RuntimeError('Cannot call "receive" once a disconnect message has been received.'),
    ],
    ids=["disconnect-message", "disconnect-exception", "receive-after-disconnect"],
)
async def test_receive_returns_none_when_connection_ends(
    ending: dict[str, Any] | Exception,
) -> None:
    assert await _transport(FakeWebSocket([ending])).receive() is None


# ---- send -----------------------------------------------------------------------------------


async def test_send_writes_json_without_null_fields() -> None:
    websocket = FakeWebSocket()

    await _transport(websocket).send(OutputFrame(data="hi"))

    assert websocket.sent == ['{"type":"output","data":"hi"}']


@pytest.mark.parametrize(
    "error", [WebSocketDisconnect(1006), RuntimeError("close sent"), ConnectionResetError()]
)
async def test_send_raises_transport_closed_when_peer_is_gone(error: Exception) -> None:
    websocket = FakeWebSocket()
    websocket.send_error = error

    with pytest.raises(TransportClosedError):
        await _transport(websocket).send(OutputFrame(data="hi"))


# ---- close ----------------------------------------------------------------------------------


async def test_close_closes_websocket() -> None:
    websocket = FakeWebSocket()

    await _transport(websocket).close()

    assert websocket.close_calls == 1


@pytest.mark.parametrize(
    "error", [WebSocketDisconnect(1006), RuntimeError("already closed"), ConnectionResetError()]
)
async def test_close_ignores_peer_that_left_first(error: Exception) -> None:
    """클라이언트가 error 프레임을 받자마자 떠나면 서버의 close 와 엇갈린다. 예외가 새면 안 된다."""
    websocket = FakeWebSocket()
    websocket.close_error = error

    await _transport(websocket).close()
