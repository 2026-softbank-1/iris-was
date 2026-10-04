"""Build Worker — BUILD job 을 선점해 CodeBuild 빌드를 시작하고 결과(image digest)를 기록한다.

같은 프로세스에서 레포 구성 분석(repository_analyses)도 선점해 분석기를 실행한다. 빌드와 같은
GitHub App 으로 소스를 받으며, 빌드 슬롯과 따로 센다.
"""

import asyncio
import logging
import os
import signal
import socket
from collections.abc import Coroutine
from typing import Any

import httpx
from sqlalchemy.exc import SQLAlchemyError

from app.clients.analysis_gate_client import SubprocessAnalysisGateClient
from app.clients.aws_clients import (
    ArtifactStore,
    CloudWatchBuildLogClient,
    CodeBuildClient,
    EcrClient,
)
from app.clients.github_client import GITHUB_API_URL, GitHubClient
from app.core.config import get_build_worker_settings, get_settings
from app.core.database import get_session_factory
from app.core.exceptions import BuildFailedError
from app.core.logging import build_extra, configure_logging, log_context
from app.models import Job, RepositoryAnalysis
from app.services.analysis_gate_service import AnalysisGateService
from app.services.build_service import JOB_KINDS, BuildService
from app.workers.job_wakeup import JobWakeup

logger = logging.getLogger(__name__)

# 분석 트리거가 `NOTIFY jobs, <payload>` 로 보내는 값.
ANALYSIS_WAKEUP_KINDS = frozenset({"REPOSITORY_ANALYSIS"})


async def run(
    stop: asyncio.Event, service: BuildService, concurrency: int, wakeup: JobWakeup
) -> None:
    """빈 슬롯마다 job 을 선점해 처리한다. stop 이 켜지면 진행 중 job 을 반납하고 끝낸다."""
    logger.info("worker started", extra={"action": "run"})
    slots = asyncio.Semaphore(concurrency)
    tasks: set[asyncio.Task[None]] = set()
    while not stop.is_set():
        await slots.acquire()
        wakeup.clear()
        job = await _claim(service, wakeup) if not stop.is_set() else None
        if job is None:
            slots.release()
            await wakeup.wait(stop)
            continue
        task = asyncio.create_task(_process(service, job, stop))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        task.add_done_callback(lambda _: slots.release())
    await asyncio.gather(*tasks)
    logger.info("worker stopped", extra={"action": "run"})


async def _claim(service: BuildService, wakeup: JobWakeup) -> Job | None:
    try:
        return await service.claim_next_job()
    except (SQLAlchemyError, OSError):
        logger.exception("job claim failed", extra={"action": "claim_next_job"})
        wakeup.retry_soon()
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
                extra=build_extra(
                    {"action": "process_job", "failure_code": exc.failure_code}, exc.fields
                ),
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


async def run_analyses(
    stop: asyncio.Event, service: AnalysisGateService, concurrency: int, wakeup: JobWakeup
) -> None:
    """빈 슬롯마다 분석을 선점해 실행한다. stop 이 켜지면 진행 중 분석을 멈추고 대기열로 돌린다."""
    logger.info("analysis loop started", extra={"action": "run_analyses"})
    slots = asyncio.Semaphore(concurrency)
    tasks: set[asyncio.Task[None]] = set()

    async def cancel_on_stop() -> None:
        await stop.wait()
        for task in tasks:
            task.cancel()

    stopper = asyncio.create_task(cancel_on_stop())
    try:
        while not stop.is_set():
            await slots.acquire()
            wakeup.clear()
            analysis = await _claim_analysis(service, wakeup) if not stop.is_set() else None
            if analysis is None:
                slots.release()
                await wakeup.wait(stop)
                continue
            task = asyncio.create_task(_process_analysis(service, analysis))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
            task.add_done_callback(lambda _: slots.release())
            if stop.is_set():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        stopper.cancel()
    logger.info("analysis loop stopped", extra={"action": "run_analyses"})


async def _claim_analysis(
    service: AnalysisGateService, wakeup: JobWakeup
) -> RepositoryAnalysis | None:
    try:
        return await service.claim_next_analysis()
    except (SQLAlchemyError, OSError):
        logger.exception("analysis claim failed", extra={"action": "claim_next_analysis"})
        wakeup.retry_soon()
        return None


async def _process_analysis(service: AnalysisGateService, analysis: RepositoryAnalysis) -> None:
    with log_context(repository_analysis_id=analysis.id):
        try:
            await service.run(analysis)
        except asyncio.CancelledError:
            # 종료 신호다. 분석기 프로세스는 클라이언트가 이미 정리했다. 바로 다른 Worker 가
            # 이어 가도록 반납한다. 반납마저 실패하면 lease 만료 뒤 다시 선점된다.
            try:
                await asyncio.shield(service.release(analysis.id))
            except Exception:
                logger.exception("analysis not released", extra={"action": "process_analysis"})
            raise
        except Exception:
            # 결과를 기록하지 못했다. lease 가 만료되면 다른 Worker 가 처음부터 다시 실행한다.
            logger.exception("analysis attempt failed", extra={"action": "process_analysis"})


async def main() -> None:
    settings = get_build_worker_settings()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    # SIGTERM(K8s Pod 종료)을 받으면 빌드를 기다리던 job 을 반납하고 끝낸다.
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    async with httpx.AsyncClient(base_url=GITHUB_API_URL, timeout=30.0) as http:
        artifacts = ArtifactStore(settings.aws_region, settings.artifact_bucket)
        github = GitHubClient(
            http, settings.github_app_id, settings.github_app_private_key.get_secret_value()
        )
        # Pod 이름(hostname)으로 lease 소유자를 구분한다.
        worker_id = f"{socket.gethostname()}:{os.getpid()}"
        service = BuildService(
            session_factory=get_session_factory(),
            github=github,
            codebuild=CodeBuildClient(settings.aws_region, settings.codebuild_project),
            ecr=EcrClient(settings.aws_region),
            artifacts=artifacts,
            uploads=artifacts,
            build_logs=CloudWatchBuildLogClient(settings.aws_region),
            settings=settings,
            worker_id=worker_id,
        )
        analysis_service = AnalysisGateService(
            session_factory=get_session_factory(),
            github=github,
            analyzer=SubprocessAnalysisGateClient(
                settings.analysis_gate_command,
                timeout_seconds=settings.analysis_gate_timeout_seconds,
            ),
            settings=settings,
            worker_id=worker_id,
        )
        wakeup = JobWakeup(JOB_KINDS, service.find_seconds_until_next_run)
        analysis_wakeup = JobWakeup(
            ANALYSIS_WAKEUP_KINDS, analysis_service.find_seconds_until_next_run
        )
        try:
            await asyncio.gather(
                run(stop, service, settings.concurrency, wakeup),
                run_analyses(
                    stop, analysis_service, settings.analysis_gate_concurrency, analysis_wakeup
                ),
            )
        finally:
            await wakeup.close()
            await analysis_wakeup.close()


if __name__ == "__main__":
    configure_logging("build-worker", get_settings().log_level)
    asyncio.run(main())
