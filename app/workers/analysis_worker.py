"""Independent source-analysis worker. Does not execute builds or deployments."""

import asyncio
import contextlib
import logging
import signal

import httpx

from app.clients.analysis_source_client import GithubAnalysisSourceClient
from app.clients.analyzer_client import LocalAnalyzerClient
from app.clients.source_repository_client import GithubSourceRepositoryClient
from app.core.analysis_config import get_analysis_settings
from app.core.config import get_settings
from app.core.database import get_session_factory
from app.core.exceptions import NotConfiguredError
from app.core.logging import configure_logging
from app.services.analysis_worker_service import AnalysisWorkerService

logger = logging.getLogger(__name__)


async def run(
    stop: asyncio.Event, service: AnalysisWorkerService, poll_interval: float = 2
) -> None:
    logger.info("analysis worker started", extra={"action": "run"})
    while not stop.is_set():
        try:
            analysis = await service.claim_next_analysis()
            if analysis is None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=poll_interval)
                continue
            task = asyncio.create_task(service.process_analysis(analysis))
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
            # Do not log exception text from source/model clients.
            logger.error("analysis worker attempt failed", extra={"action": "run"})
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=poll_interval)
    logger.info("analysis worker stopped", extra={"action": "run"})


async def main() -> None:
    settings = get_settings()
    analysis_settings = get_analysis_settings()
    if settings.github_app_id is None or settings.github_app_private_key is None:
        raise NotConfiguredError("github app is not configured")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    async with httpx.AsyncClient(timeout=30) as http:
        source_client = GithubAnalysisSourceClient(
            http,
            GithubSourceRepositoryClient(
                http,
                settings.github_app_id,
                settings.github_app_private_key.get_secret_value(),
                settings.github_api_base_url,
            ),
            settings.github_api_base_url,
        )
        analyzer = LocalAnalyzerClient(
            config=analysis_settings.model_config_values(),
            executable=analysis_settings.executable,
            budget_ledger=analysis_settings.budget_ledger,
            max_cost_usd=analysis_settings.max_cost_usd,
        )
        service = AnalysisWorkerService(
            get_session_factory(), source_client, analyzer, analysis_settings
        )
        try:
            await run(stop, service, analysis_settings.poll_interval_seconds)
        finally:
            await get_session_factory().kw["bind"].dispose()


if __name__ == "__main__":
    configure_logging("analysis-worker", get_settings().log_level)
    asyncio.run(main())
