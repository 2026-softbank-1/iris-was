"""Starlette WebSocket 을 서비스가 보는 `ConsoleTransport` 로 맞춘다."""

import asyncio
import logging

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from app.schemas.console import (
    CONSOLE_CLIENT_FRAME_ADAPTER,
    ConsoleClientFrame,
    ConsoleServerFrame,
)
from app.services.console_gateway_service import TransportClosedError

logger = logging.getLogger(__name__)


class WebSocketTransport:
    def __init__(self, websocket: WebSocket) -> None:
        self._websocket = websocket
        # 입력 쪽(pong)과 출력 쪽(output)이 같은 소켓에 동시에 쓰지 않게 한다.
        self._send_lock = asyncio.Lock()

    async def receive(self) -> ConsoleClientFrame | None:
        while True:
            try:
                message = await self._websocket.receive()
            except (WebSocketDisconnect, RuntimeError):
                return None
            if message["type"] == "websocket.disconnect":
                return None
            text = message.get("text")
            if text is None:
                # 이 프로토콜은 JSON 텍스트 프레임만 쓴다.
                logger.warning("binary console frame ignored", extra={"action": "receive_frame"})
                continue
            try:
                return CONSOLE_CLIENT_FRAME_ADAPTER.validate_json(text)
            except ValidationError:
                # 입력 내용이 프레임에 담겨 있을 수 있어 오류 상세를 남기지 않는다.
                logger.warning("invalid console frame ignored", extra={"action": "receive_frame"})

    async def send(self, frame: ConsoleServerFrame) -> None:
        async with self._send_lock:
            try:
                await self._websocket.send_text(frame.model_dump_json(exclude_none=True))
            except (WebSocketDisconnect, RuntimeError, OSError) as exc:
                raise TransportClosedError from exc

    async def close(self) -> None:
        try:
            await self._websocket.close()
        except (RuntimeError, OSError):
            return
