"""Worker 깨우기. jobs 트리거의 `NOTIFY jobs, <kind>` 를 듣고, 가장 이른 미래 run_after 에도
깨어난다.

알림은 저장되지 않는다. 리스너 재연결·Pod 재시작 중 놓친 알림과 만료된 lease 는
FALLBACK_SECONDS 마다 다시 선점을 시도해 회수한다.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import asyncpg
from sqlalchemy.exc import SQLAlchemyError

from app.core.database import connect_listener
from app.enums import JobKind

logger = logging.getLogger(__name__)

FALLBACK_SECONDS = 60.0
# 리스너가 없거나 DB 오류가 났을 때는 알림을 믿을 수 없어 예전 polling 간격으로 다시 시도한다.
ERROR_RETRY_SECONDS = 5.0
_PING_TIMEOUT_SECONDS = 5.0
_LISTENER_ERRORS = (OSError, asyncpg.PostgresError, asyncpg.InterfaceError)


class JobWakeup:
    def __init__(
        self,
        kinds: frozenset[JobKind],
        find_seconds_until_next_run: Callable[[], Awaitable[float | None]],
    ) -> None:
        self._kinds = kinds
        self._find_seconds_until_next_run = find_seconds_until_next_run
        self._notified = asyncio.Event()
        self._connection: asyncpg.Connection | None = None
        self._retry_timeout: float | None = None

    def clear(self) -> None:
        """선점 전에 부른다. 선점하는 동안 온 알림은 남아 있다가 다음 wait 를 바로 끝낸다."""
        self._notified.clear()

    def retry_soon(self) -> None:
        """선점이 DB 오류로 실패했을 때 부른다. 다음 wait 는 ERROR_RETRY_SECONDS 만 기다린다."""
        self._retry_timeout = ERROR_RETRY_SECONDS

    async def wait(self, stop: asyncio.Event) -> None:
        """알림·다음 run_after·FALLBACK_SECONDS·stop 중 먼저 오는 것까지 기다린다."""
        timeout, self._retry_timeout = self._retry_timeout, None
        if stop.is_set():
            return
        if not await self._is_listening():
            await self._listen()
            if self._connection is not None:
                return  # 방금 LISTEN 을 시작했다. 그 전에 온 알림을 놓쳤을 수 있어 바로 선점한다.
            timeout = ERROR_RETRY_SECONDS
        if timeout is None:
            timeout = await self._next_timeout()
        waits = [asyncio.create_task(self._notified.wait()), asyncio.create_task(stop.wait())]
        await asyncio.wait(waits, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        for task in waits:
            task.cancel()

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()

    async def _next_timeout(self) -> float:
        try:
            seconds = await self._find_seconds_until_next_run()
        except (SQLAlchemyError, OSError):
            logger.exception("next run lookup failed", extra={"action": "find_next_run"})
            return ERROR_RETRY_SECONDS
        return FALLBACK_SECONDS if seconds is None else min(seconds, FALLBACK_SECONDS)

    async def _is_listening(self) -> bool:
        # 장애 조치·네트워크 단절로 RST 없이 끊긴 연결은 is_closed() 로 알 수 없어 직접 확인한다.
        if self._connection is None:
            return False
        try:
            await self._connection.execute("SELECT 1", timeout=_PING_TIMEOUT_SECONDS)
            return True
        except _LISTENER_ERRORS:
            logger.warning("job listener lost", extra={"action": "listen"}, exc_info=True)
            self._connection.terminate()
            self._connection = None
            return False

    async def _listen(self) -> None:
        try:
            self._connection = await connect_listener()
            await self._connection.add_listener("jobs", self._on_notify)
        except _LISTENER_ERRORS:
            logger.warning("job listener unavailable", extra={"action": "listen"}, exc_info=True)
            if self._connection is not None:
                self._connection.terminate()
                self._connection = None

    def _on_notify(self, connection: Any, pid: int, channel: str, payload: str) -> None:
        if payload in self._kinds:
            self._notified.set()
