"""GitOps queue entry point with isolated write and read-only Argo credentials."""

import asyncio
import logging
import signal
from uuid import uuid4

import httpx

from app.clients.argocd_client import ArgoCdClient
from app.clients.aws_clients import EcrClient
from app.clients.gitops_client import GithubGitOpsClient
from app.clients.source_repository_client import GithubSourceRepositoryClient
from app.core.async_io import run_sync
from app.core.config import get_settings
from app.core.database import get_session_factory
from app.core.deploy_config import get_deploy_worker_settings
from app.core.logging import configure_logging, log_context
from app.services.deploy_service import DeployService

logger = logging.getLogger(__name__)


async def run(stop: asyncio.Event) -> None:
    if stop.is_set():
        return
    settings = get_deploy_worker_settings()
    worker_id = "deploy-" + uuid4().hex
    async with (
        httpx.AsyncClient(timeout=60) as github_http,
        httpx.AsyncClient(
            base_url=settings.argocd_server_url,
            headers={"Authorization": f"Bearer {settings.argocd_token.get_secret_value()}"},
            timeout=30,
        ) as argo_http,
    ):
        credentials = GithubSourceRepositoryClient(
            github_http,
            settings.gitops_app_id,
            settings.gitops_app_private_key.get_secret_value(),
            settings.github_api_base_url,
        )
        service = DeployService(
            get_session_factory(),
            GithubGitOpsClient(
                github_http,
                credentials,
                settings.gitops_installation_id,
                settings.gitops_repository,
                settings.gitops_branch,
                settings.github_api_base_url,
            ),
            ArgoCdClient(argo_http, settings.gitops_repository),
            settings,
            worker_id,
            await run_sync(lambda: EcrClient(settings.aws_region)),
        )
        logger.info("worker started", extra={"action": "run"})
        async with asyncio.TaskGroup() as group:
            for _ in range(settings.concurrency):
                group.create_task(run_slot(service, stop, settings.poll_interval_seconds))
    logger.info("worker stopped", extra={"action": "run"})


async def run_slot(service: DeployService, stop: asyncio.Event, interval: float) -> None:
    while not stop.is_set():
        try:
            job = await service.claim_next_job()
            if job is not None:
                with log_context(
                    job_id=job.id,
                    job_kind=job.kind,
                    deployment_request_id=job.deployment_request_id,
                ):
                    await service.process_job(job)
                continue
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("deploy queue processing failed", extra={"action": "run_deploy_slot"})
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
    configure_logging("deploy-worker", get_settings().log_level)
    asyncio.run(main())
