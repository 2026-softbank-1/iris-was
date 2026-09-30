"""Build Worker — BUILD job 을 선점해 CodeBuild 빌드를 시작하고 결과(image digest)를 기록한다."""

import asyncio
import contextlib
import logging
import signal

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.enums import JobKind

logger = logging.getLogger(__name__)

JOB_KINDS = frozenset({JobKind.BUILD})
POLL_INTERVAL_SECONDS = 5.0


async def run(stop: asyncio.Event) -> None:
    logger.info("worker started", extra={"action": "run"})
    while not stop.is_set():
        # TODO: JOB_KINDS job 을 선점(claim)해
        #   with log_context(job_id=..., job_kind=..., deployment_request_id=...) 안에서 처리한다.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=POLL_INTERVAL_SECONDS)
    logger.info("worker stopped", extra={"action": "run"})


async def main() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    # SIGTERM(K8s Pod 종료)을 받으면 진행 중 작업을 마치고 루프를 빠져나온다.
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await run(stop)


if __name__ == "__main__":
    configure_logging("build-worker", get_settings().log_level)
    asyncio.run(main())
