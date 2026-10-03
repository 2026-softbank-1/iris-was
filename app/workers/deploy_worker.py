"""Deploy Worker — DEPLOY·ROLLBACK·RECONCILE job 을 선점해 GitOps 저장소를 바꾸고
Argo CD 상태를 수집한다. 사용자가 등록한 온프레미스 서버의 GitOps 반영·연결 확인도 한다.
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
from app.clients.secret_sealer import SecretSealer
from app.core.config import get_deploy_worker_settings, get_settings
from app.core.crypto import VariableCipher
from app.core.database import get_session_factory
from app.core.logging import configure_logging, log_context
from app.models import Job, OnpremServer
from app.services.deploy_service import JOB_KINDS, DeployService
from app.services.gitops_writer import GitOpsWriter
from app.services.onprem_server_sync_service import OnpremServerSyncService
from app.workers.job_wakeup import JobWakeup

logger = logging.getLogger(__name__)


async def run(
    stop: asyncio.Event,
    service: DeployService,
    wakeup: JobWakeup,
    servers: OnpremServerSyncService | None = None,
) -> None:
    """job 과 서버를 하나씩 번갈아 처리한다. 둘 다 짧게 끝나므로 stop 이 켜지면 지금 것을 마치고
    끝낸다. 서버 동기화가 꺼져 있으면(servers 가 None) job 만 처리한다.
    """
    # ponytail: job 을 한 번에 하나만 처리한다. 진행 중 release 가 많아 RECONCILE 이 밀리면
    #   build_worker 처럼 Semaphore 로 동시 처리하거나 replica 를 늘린다.
    logger.info("worker started", extra={"action": "run"})
    while not stop.is_set():
        wakeup.clear()
        job = await _claim(service, wakeup)
        if job is not None:
            await _process(service, job)
        server = await _claim_server(servers, wakeup) if servers is not None else None
        if server is not None and servers is not None:
            await _sync_server(servers, server)
        if job is None and server is None:
            await wakeup.wait(stop)
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


async def _claim_server(servers: OnpremServerSyncService, wakeup: JobWakeup) -> OnpremServer | None:
    try:
        return await servers.claim_next_server()
    except (SQLAlchemyError, OSError):
        logger.exception("onprem server claim failed", extra={"action": "claim_next_server"})
        wakeup.retry_soon()
        return None


async def _sync_server(servers: OnpremServerSyncService, server: OnpremServer) -> None:
    with log_context(onprem_server_id=server.id):
        try:
            await servers.run(server)
        except Exception as exc:
            logger.exception("onprem server sync failed", extra={"action": "sync_server"})
            # 기록마저 실패하면 lease 만료 후 다시 가져간다.
            try:
                await servers.retry_later(server, exc)
            except Exception:
                logger.exception(
                    "onprem server failure not recorded", extra={"action": "sync_server"}
                )


async def _seconds_until_next(
    service: DeployService, servers: OnpremServerSyncService | None
) -> float | None:
    """다음 job 의 run_after 와 다음 서버 확인 시각 중 이른 쪽까지 남은 초."""
    candidates = [await service.find_seconds_until_next_run()]
    if servers is not None:
        candidates.append(await servers.find_seconds_until_next_check())
    seconds = [value for value in candidates if value is not None]
    return min(seconds) if seconds else None


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
        github = GitHubClient(
            github_http,
            settings.gitops_app_id,
            settings.gitops_app_private_key.get_secret_value(),
        )
        argocd = ArgoCdClient(argocd_http, settings.gitops_repository)
        cipher = (
            VariableCipher(settings.variables_encryption_key.get_secret_value())
            if settings.variables_encryption_key is not None
            else None
        )
        worker_id = f"{socket.gethostname()}:{os.getpid()}"
        service = DeployService(
            session_factory=get_session_factory(),
            github=github,
            argocd=argocd,
            ecr=EcrClient(settings.aws_region),
            settings=settings,
            worker_id=worker_id,
            cipher=cipher,
            sealer=(
                SecretSealer(settings.sealed_secrets_cert) if settings.sealed_secrets_cert else None
            ),
        )
        servers = None
        if settings.platform_sealed_secrets_cert:
            servers = OnpremServerSyncService(
                get_session_factory(),
                github,
                GitOpsWriter(github, settings.gitops_installation_id, settings.gitops_repository),
                argocd,
                worker_id,
                platform_sealer=SecretSealer(
                    settings.platform_sealed_secrets_cert, setting="PLATFORM_SEALED_SECRETS_CERT"
                ),
                cipher=cipher,
            )
        else:
            logger.warning(
                "onprem server sync is off",
                extra={"action": "main", "setting": "PLATFORM_SEALED_SECRETS_CERT"},
            )

        async def seconds_until_next() -> float | None:
            return await _seconds_until_next(service, servers)

        wakeup = JobWakeup(JOB_KINDS, seconds_until_next)
        try:
            await run(stop, service, wakeup, servers)
        finally:
            await wakeup.close()


if __name__ == "__main__":
    configure_logging("deploy-worker", get_settings().log_level)
    asyncio.run(main())
