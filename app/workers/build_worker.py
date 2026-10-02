"""BUILD queue entry point; source/build side effects stay in worker processes."""

import asyncio
import logging
import signal
from uuid import uuid4

import httpx

from app.clients.analysis_source_client import GithubAnalysisSourceClient
from app.clients.aws_clients import ArtifactStore, CodeBuildClient, EcrClient
from app.clients.source_repository_client import GithubSourceRepositoryClient
from app.core.async_io import run_sync
from app.core.build_config import get_build_worker_settings
from app.core.config import get_settings
from app.core.database import get_session_factory
from app.core.logging import configure_logging, log_context
from app.services.build_service import BuildService

logger = logging.getLogger(__name__)


async def run(stop: asyncio.Event) -> None:
    if stop.is_set():
        return
    settings = get_build_worker_settings()
    worker_id = "build-" + uuid4().hex
    async with httpx.AsyncClient(timeout=60) as http:
        credentials = GithubSourceRepositoryClient(
            http,
            settings.github_app_id,
            settings.github_app_private_key.get_secret_value(),
            settings.github_api_base_url,
        )
        clients = await run_sync(
            lambda: (
                CodeBuildClient(settings.aws_region, settings.codebuild_project),
                EcrClient(settings.aws_region),
                ArtifactStore(settings.aws_region, settings.artifact_bucket),
            )
        )
        service = BuildService(
            get_session_factory(),
            GithubAnalysisSourceClient(http, credentials, settings.github_api_base_url),
            *clients,
            settings,
            worker_id,
        )
        logger.info("worker started", extra={"action": "run"})
        async with asyncio.TaskGroup() as group:
            for _ in range(settings.concurrency):
                group.create_task(run_slot(service, stop, settings.poll_interval_seconds))
    logger.info("worker stopped", extra={"action": "run"})


async def run_slot(service: BuildService, stop: asyncio.Event, interval: float) -> None:
    while not stop.is_set():
        try:
            job = await service.claim_next_job()
            if job is not None:
                with log_context(
                    job_id=job.id,
                    job_kind=job.kind,
                    deployment_request_id=job.deployment_request_id,
                ):
                    await service.process_job(job, stop)
                continue
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("build queue processing failed", extra={"action": "run_build_slot"})
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            continue


async def main() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await run(stop)


if __name__ == "__main__":
    configure_logging("build-worker", get_settings().log_level)
    asyncio.run(main())
