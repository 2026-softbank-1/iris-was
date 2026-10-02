"""Advance durable pipelines without running cloud or model calls in request handlers."""

import asyncio
import contextlib
import signal

import httpx

from app.clients.analysis_source_client import GithubAnalysisSourceClient
from app.clients.analyzer_client import LocalAnalyzerClient
from app.clients.source_repository_client import GithubSourceRepositoryClient
from app.core.analysis_config import get_analysis_settings
from app.core.config import get_settings
from app.core.database import get_engine, get_session_factory
from app.core.exceptions import NotConfiguredError
from app.core.logging import configure_logging
from app.services.pipeline_worker_service import PipelineWorkerService


async def main() -> None:
    settings = get_settings()
    analysis = get_analysis_settings()
    if not settings.github_app_id or not settings.github_app_private_key:
        raise NotConfiguredError("github source access is not configured")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    async with httpx.AsyncClient(timeout=30) as http:
        github = GithubSourceRepositoryClient(
            http,
            settings.github_app_id,
            settings.github_app_private_key.get_secret_value(),
            settings.github_api_base_url,
        )
        worker = PipelineWorkerService(
            get_session_factory(),
            GithubAnalysisSourceClient(http, github, settings.github_api_base_url),
            LocalAnalyzerClient(
                config=analysis.model_config_values(),
                budget_ledger=analysis.budget_ledger,
                executable=analysis.executable,
                max_cost_usd=analysis.max_cost_usd,
            ),
            analysis,
        )
        try:
            while not stop.is_set():
                task = asyncio.create_task(worker.tick())
                stopping = asyncio.create_task(stop.wait())
                try:
                    completed, _ = await asyncio.wait(
                        (task, stopping), return_when=asyncio.FIRST_COMPLETED
                    )
                    if stopping in completed and not task.done():
                        task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                finally:
                    stopping.cancel()
                    await asyncio.gather(stopping, return_exceptions=True)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), analysis.poll_interval_seconds)
        finally:
            await get_engine().dispose()


if __name__ == "__main__":
    configure_logging("pipeline-worker", get_settings().log_level)
    asyncio.run(main())
