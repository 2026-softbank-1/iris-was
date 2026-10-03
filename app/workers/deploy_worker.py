"""Deploy Worker — DEPLOY·ROLLBACK·RECONCILE job 을 선점해 GitOps 저장소를 바꾸고
Argo CD 상태를 수집한다.
"""

import asyncio
import logging
import os
import signal
import socket

import httpx
from sqlalchemy.exc import SQLAlchemyError

from app.clients.argocd_client import ArgoCdClient
from app.clients.aws_clients import EcrClient
from app.clients.github_client import GITHUB_API_URL, GitHubClient
from app.core.config import get_deploy_worker_settings, get_settings
from app.core.database import get_session_factory
from app.core.logging import configure_logging, log_context
from app.models import Job
from app.services.deploy_service import JOB_KINDS, DeployService
from app.workers.job_wakeup import JobWakeup

logger = logging.getLogger(__name__)


async def run(stop: asyncio.Event, service: DeployService, wakeup: JobWakeup) -> None:
    """job 을 하나씩 처리한다. job 은 짧게 끝나므로 stop 이 켜지면 현재 job 을 마치고 끝낸다."""
    # ponytail: job 을 한 번에 하나만 처리한다. 진행 중 release 가 많아 RECONCILE 이 밀리면
    #   build_worker 처럼 Semaphore 로 동시 처리하거나 replica 를 늘린다.
    logger.info("worker started", extra={"action": "run"})
    while not stop.is_set():
        wakeup.clear()
        job = await _claim(service, wakeup)
        if job is None:
            await wakeup.wait(stop)
            continue
        await _process(service, job)
    logger.info("worker stopped", extra={"action": "run"})


async def _claim(service: DeployService, wakeup: JobWakeup) -> Job | None:
    try:
        return await service.claim_next_job()
    except (SQLAlchemyError, OSError):
        logger.exception("job claim failed", extra={"action": "claim_next_job"})
        wakeup.retry_soon()
        return None


async def _process(service: DeployService, job: Job) -> None:
    with log_context(
        job_id=job.id, job_kind=job.kind, deployment_request_id=job.deployment_request_id
    ):
        try:
            await service.run(job)
        except Exception as exc:
            logger.exception("job attempt failed", extra={"action": "process_job"})
            # 실패 기록마저 실패하면 lease 만료 후 다른 Worker 가 다시 가져간다.
            try:
                await service.retry_or_fail(job, exc)
            except Exception:
                logger.exception("job failure not recorded", extra={"action": "process_job"})


async def main() -> None:
    settings = get_deploy_worker_settings()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    # SIGTERM(K8s Pod 종료)을 받으면 진행 중 job 을 마치고 루프를 빠져나온다.
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    argocd_headers = {"Authorization": f"Bearer {settings.argocd_token.get_secret_value()}"}
    async with (
        httpx.AsyncClient(base_url=GITHUB_API_URL, timeout=30.0) as github_http,
        httpx.AsyncClient(
            base_url=settings.argocd_server_url, headers=argocd_headers, timeout=60.0
        ) as argocd_http,
    ):
        service = DeployService(
            session_factory=get_session_factory(),
            github=GitHubClient(
                github_http,
                settings.gitops_app_id,
                settings.gitops_app_private_key.get_secret_value(),
            ),
            argocd=ArgoCdClient(argocd_http, settings.gitops_repository),
            ecr=EcrClient(settings.aws_region),
            settings=settings,
            worker_id=f"{socket.gethostname()}:{os.getpid()}",
        )
        wakeup = JobWakeup(JOB_KINDS, service.find_seconds_until_next_run)
        try:
            await run(stop, service, wakeup)
        finally:
            await wakeup.close()


if __name__ == "__main__":
    configure_logging("deploy-worker", get_settings().log_level)
    asyncio.run(main())
