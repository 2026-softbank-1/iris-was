"""Build Worker — BUILD job 을 선점해 CodeBuild 빌드를 시작하고 결과(image digest)를 기록한다."""

import asyncio
import contextlib
import logging
import os
import signal
import socket
from collections.abc import Coroutine
from typing import Any

import httpx
from sqlalchemy.exc import SQLAlchemyError

from app.clients.aws_clients import ArtifactStore, CodeBuildClient, EcrClient
from app.clients.github_client import GITHUB_API_URL, GitHubClient
from app.core.config import get_build_worker_settings, get_settings
from app.core.database import get_session_factory
from app.core.exceptions import BuildFailedError
from app.core.logging import configure_logging, log_context
from app.models import Job
from app.services.build_service import BuildService

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5.0


async def run(stop: asyncio.Event, service: BuildService, concurrency: int) -> None:
    """빈 슬롯마다 job 을 선점해 처리한다. stop 이 켜지면 진행 중 job 을 반납하고 끝낸다."""
    logger.info("worker started", extra={"action": "run"})
    slots = asyncio.Semaphore(concurrency)
    tasks: set[asyncio.Task[None]] = set()
    while not stop.is_set():
        await slots.acquire()
        job = await _claim(service) if not stop.is_set() else None
        if job is None:
            slots.release()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=POLL_INTERVAL_SECONDS)
            continue
        task = asyncio.create_task(_process(service, job, stop))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        task.add_done_callback(lambda _: slots.release())
    await asyncio.gather(*tasks)
    logger.info("worker stopped", extra={"action": "run"})


async def _claim(service: BuildService) -> Job | None:
    try:
        return await service.claim_next_job()
    except (SQLAlchemyError, OSError):
        logger.exception("job claim failed", extra={"action": "claim_next_job"})
        return None


async def _process(service: BuildService, job: Job, stop: asyncio.Event) -> None:
    with log_context(
        job_id=job.id, job_kind=job.kind, deployment_request_id=job.deployment_request_id
    ):
        try:
            await service.run(job, stop)
        except BuildFailedError as exc:
            logger.info(
                "build failed",
                extra={"action": "process_job", "failure_code": exc.failure_code, **exc.fields},
            )
            await _record_failure(service.fail(job, exc))
        except Exception as exc:
            logger.exception("job attempt failed", extra={"action": "process_job"})
            await _record_failure(service.retry_or_fail(job, exc))


async def _record_failure(record: Coroutine[Any, Any, None]) -> None:
    # 실패 기록마저 실패하면 lease 만료 후 다른 Worker 가 다시 가져간다.
    try:
        await record
    except Exception:
        logger.exception("job failure not recorded", extra={"action": "process_job"})


async def main() -> None:
    settings = get_build_worker_settings()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    # SIGTERM(K8s Pod 종료)을 받으면 빌드를 기다리던 job 을 반납하고 끝낸다.
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    async with httpx.AsyncClient(base_url=GITHUB_API_URL, timeout=30.0) as http:
        service = BuildService(
            session_factory=get_session_factory(),
            github=GitHubClient(
                http, settings.github_app_id, settings.github_app_private_key.get_secret_value()
            ),
            codebuild=CodeBuildClient(settings.aws_region, settings.codebuild_project),
            ecr=EcrClient(settings.aws_region),
            artifacts=ArtifactStore(settings.aws_region, settings.artifact_bucket),
            settings=settings,
            # Pod 이름(hostname)으로 lease 소유자를 구분한다.
            worker_id=f"{socket.gethostname()}:{os.getpid()}",
        )
        await run(stop, service, settings.concurrency)


if __name__ == "__main__":
    configure_logging("build-worker", get_settings().log_level)
    asyncio.run(main())
