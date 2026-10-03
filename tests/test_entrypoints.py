import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.database import get_engine
from app.enums import JobKind
from app.main import app
from app.workers import build_worker, deploy_worker, job_wakeup


async def test_check_health_returns_no_content() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/healthz")

    assert response.status_code == 204


async def test_check_readiness_database_unreachable_returns_503() -> None:
    unreachable = create_async_engine("postgresql+asyncpg://u:p@127.0.0.1:1/db")
    app.dependency_overrides[get_engine] = lambda: unreachable
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/readyz")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503


async def test_build_worker_run_stops_when_event_set() -> None:
    stop = asyncio.Event()
    stop.set()

    # stop 이 이미 켜져 있어 service 를 쓰지 않는다.
    run = build_worker.run(stop, service=None, concurrency=1, wakeup=None)  # type: ignore[arg-type]
    await asyncio.wait_for(run, timeout=1)


async def test_deploy_worker_run_stops_on_signal_while_waiting() -> None:
    class NoJobService:
        async def claim_next_job(self) -> None:
            return None

    class StopOnlyWakeup:
        def clear(self) -> None:
            pass

        async def wait(self, stop: asyncio.Event) -> None:
            await stop.wait()

    stop = asyncio.Event()
    run = deploy_worker.run(stop, NoJobService(), StopOnlyWakeup())  # type: ignore[arg-type]
    task = asyncio.create_task(run)
    await asyncio.sleep(0.05)
    stop.set()

    await asyncio.wait_for(task, timeout=1)


async def test_job_wakeup_relistens_immediately_and_polls_when_listener_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeConnection:
        async def add_listener(self, channel: str, callback: object) -> None:
            pass

        async def execute(self, *args: object, **kwargs: object) -> None:
            pass

        def terminate(self) -> None:
            pass

    connections: list[object] = [OSError("db down"), FakeConnection()]

    async def connect() -> object:
        result = connections.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def never_due() -> float | None:
        return None

    monkeypatch.setattr(job_wakeup, "connect_listener", connect)
    monkeypatch.setattr(job_wakeup, "ERROR_RETRY_SECONDS", 0.01)
    wakeup = job_wakeup.JobWakeup(frozenset({JobKind.BUILD}), never_due)
    stop = asyncio.Event()

    # 리스너를 열지 못하면 알림 없이 ERROR_RETRY_SECONDS 만 기다린다.
    await asyncio.wait_for(wakeup.wait(stop), timeout=1)
    # 방금 LISTEN 을 시작하면 그 전 알림을 놓쳤을 수 있어 기다리지 않는다.
    await asyncio.wait_for(wakeup.wait(stop), timeout=0.05)
    # 선점이 DB 오류로 실패하면 FALLBACK 대신 짧게 기다린다.
    wakeup.retry_soon()
    await asyncio.wait_for(wakeup.wait(stop), timeout=1)
