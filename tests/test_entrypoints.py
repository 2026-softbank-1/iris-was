import asyncio

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.database import get_engine
from app.main import app
from app.workers import build_worker, deploy_worker


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
    run = build_worker.run(stop, service=None, concurrency=1)  # type: ignore[arg-type]
    await asyncio.wait_for(run, timeout=1)


async def test_deploy_worker_run_stops_on_signal_during_poll() -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(deploy_worker.run(stop))
    await asyncio.sleep(0.05)
    stop.set()

    await asyncio.wait_for(task, timeout=1)
