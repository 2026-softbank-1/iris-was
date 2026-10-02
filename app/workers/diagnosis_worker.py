"""Independent diagnosis queue consumer; never mutates build or deployment state."""

import asyncio
import contextlib
import logging
import signal

from app.clients.diagnosis_client import LocalDiagnosisClient
from app.clients.failure_log_client import FailureLogClient
from app.core.config import get_settings
from app.core.database import get_session_factory
from app.core.diagnosis_config import get_diagnosis_settings
from app.core.logging import configure_logging
from app.services.diagnosis_worker_service import DiagnosisWorkerService

logger = logging.getLogger(__name__)


async def run(
    stop: asyncio.Event, service: DiagnosisWorkerService, poll_interval: float = 2
) -> None:
    logger.info("diagnosis worker started", extra={"action": "run"})
    while not stop.is_set():
        try:
            row = await service.claim_next_diagnosis()
            if row is None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=poll_interval)
                continue
            task = asyncio.create_task(service.process_diagnosis(row))
            stopping = asyncio.create_task(stop.wait())
            try:
                done, _ = await asyncio.wait((task, stopping), return_when=asyncio.FIRST_COMPLETED)
                if stopping in done and not task.done():
                    task.cancel()
                await task
            finally:
                stopping.cancel()
                await asyncio.gather(stopping, return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("diagnosis worker failed", extra={"action": "run"})
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=poll_interval)
    logger.info("diagnosis worker stopped", extra={"action": "run"})


async def main() -> None:
    settings = get_diagnosis_settings()
    factory = get_session_factory()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    worker = DiagnosisWorkerService(
        factory, FailureLogClient(settings), LocalDiagnosisClient(settings), settings
    )
    try:
        await run(stop, worker, settings.poll_interval_seconds)
    finally:
        await factory.kw["bind"].dispose()


if __name__ == "__main__":
    configure_logging("diagnosis-worker", get_settings().log_level)
    asyncio.run(main())
