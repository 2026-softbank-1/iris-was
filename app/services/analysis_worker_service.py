"""Durable analysis execution with renewable leases and guarded terminal writes."""

import asyncio
import hashlib
import json
import logging
import re
import tempfile
from pathlib import Path
from typing import Literal, cast

from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.analysis_source_client import AnalysisSourceClient
from app.clients.analyzer_client import AnalyzerClient
from app.core.analysis_config import AnalysisSettings
from app.core.async_io import run_sync
from app.core.exceptions import AppError, InvalidAnalyzerResultError
from app.enums import AnalysisJobStatus
from app.models.service_analysis import ServiceAnalysis
from app.repositories.github_installation_repository import GithubInstallationRepository
from app.repositories.service_analysis_repository import ServiceAnalysisRepository
from app.repositories.service_repository import ServiceRepository
from app.services.analysis_service import analysis_context, source_context

logger = logging.getLogger(__name__)
_PROGRESS_STAGES = frozenset(
    {
        "snapshot",
        "preprocess",
        "analyze",
        "verify",
        "readiness",
        "dossier",
        "preprocessing",
        "analyzing",
        "expanding",
        "validating",
        "planning",
    }
)


class AnalysisWorkerService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        source_client: AnalysisSourceClient,
        analyzer_client: AnalyzerClient,
        settings: AnalysisSettings,
    ) -> None:
        self._session_factory = session_factory
        self._source_client = source_client
        self._analyzer_client = analyzer_client
        self._settings = settings

    async def claim_next_analysis(self) -> ServiceAnalysis | None:
        async with self._session_factory.begin() as session:
            return await ServiceAnalysisRepository(session).claim_next(self._settings.lease_seconds)

    async def process_analysis(self, analysis: ServiceAnalysis) -> None:
        token = analysis.lease_token
        assert token is not None
        task = asyncio.create_task(self._execute(analysis, token))
        heartbeat = asyncio.create_task(self._heartbeat(analysis.id, token, task))
        try:
            await task
        except asyncio.CancelledError:
            # API cancellation and another lease owner are final for this attempt.
            if not task.cancelled():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await self._requeue(analysis.id, token)
        except TimeoutError:
            await self._fail(analysis.id, token, "ANALYSIS_TIMED_OUT")
        except AppError as error:
            await self._fail(analysis.id, token, error.code)
            logger.warning(
                "analysis execution failed",
                extra={
                    "action": "process_analysis",
                    "analysis_id": analysis.id,
                    "code": error.code,
                },
            )
        except Exception:
            # The exception text can contain model/source secrets; record a bounded code only.
            logger.error(
                "analysis execution failed",
                extra={"action": "process_analysis", "analysis_id": analysis.id},
            )
            await self._fail(analysis.id, token, "ANALYSIS_FAILED")
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _heartbeat(self, analysis_id: str, token: str, task: asyncio.Task[None]) -> None:
        try:
            while not task.done():
                await asyncio.sleep(self._settings.lease_seconds / 3)
                async with self._session_factory.begin() as session:
                    retained = await ServiceAnalysisRepository(session).renew_lease(
                        analysis_id, token, self._settings.lease_seconds
                    )
                if not retained:
                    task.cancel()
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            # With no confirmed lease, continuing could run alongside a replacement worker.
            task.cancel()
            logger.error("analysis lease renewal failed", extra={"action": "renew_analysis_lease"})

    async def _execute(self, analysis: ServiceAnalysis, token: str) -> None:
        async with asyncio.timeout(self._settings.timeout_seconds):
            if analysis.attempts > 3:
                await self._fail(analysis.id, token, "ANALYSIS_ATTEMPTS_EXHAUSTED")
                return
            if (
                analysis.mode == "opencode"
                and analysis.model_selection != self._settings.model_selection()
            ):
                await self._fail(analysis.id, token, "MODEL_CONFIGURATION_CHANGED")
                return
            if not await self._check_context(analysis):
                await self._fail(analysis.id, token, "ANALYSIS_STALE")
                return
            head = await self._source_client.get_head_sha(
                analysis.source_repository_url,
                analysis.source_branch,
                analysis.github_installation_id,
            )
            if head != analysis.source_sha:
                await self._fail(analysis.id, token, "ANALYSIS_STALE")
                return
            # No transaction or credentials are handed to the analyzer.
            temporary = await run_sync(lambda: tempfile.TemporaryDirectory(prefix="iris-analysis-"))
            try:
                destination = await run_sync(lambda: Path(temporary.name).resolve() / "source")
                source_root = await self._source_client.fetch_source(
                    analysis.source_repository_url,
                    analysis.source_sha,
                    analysis.github_installation_id,
                    destination,
                )

                async def on_progress(stage: str) -> None:
                    if stage in _PROGRESS_STAGES:
                        await self._record_stage(analysis.id, token, stage)

                await on_progress("analyze")
                output = await self._analyzer_client.analyze(
                    source_root=source_root,
                    source_sha=analysis.source_sha,
                    root_directory=analysis.root_directory,
                    mode=cast(Literal["static", "opencode"], analysis.mode),
                    on_progress=on_progress,
                )
            finally:
                await run_sync(temporary.cleanup)
            head = await self._source_client.get_head_sha(
                analysis.source_repository_url,
                analysis.source_branch,
                analysis.github_installation_id,
            )
            if head != analysis.source_sha:
                await self._fail(analysis.id, token, "ANALYSIS_STALE")
                return
            await self._complete(analysis, token, output)

    async def _check_context(self, analysis: ServiceAnalysis) -> bool:
        async with self._session_factory.begin() as session:
            service = await ServiceRepository(session).find_by_id_and_owner_id(
                analysis.service_id, analysis.requested_by
            )
            if service is None or source_context(service) != analysis_context(analysis):
                return False
            installations = await GithubInstallationRepository(session).search_by_user_id(
                analysis.requested_by
            )
            return any(
                row.id == service.github_installation_id
                and row.installation_id == analysis.github_installation_id
                for row in installations
            )

    async def _record_stage(self, analysis_id: str, token: str, stage: str) -> None:
        async with self._session_factory.begin() as session:
            row = await ServiceAnalysisRepository(session).find_running(
                analysis_id, token, for_update=True
            )
            if row is None:
                raise asyncio.CancelledError
            row.stage = stage

    async def _complete(
        self, analysis: ServiceAnalysis, token: str, output: dict[str, JsonValue]
    ) -> None:
        async with self._session_factory.begin() as session:
            service = await ServiceRepository(session).find_by_id_and_owner_id_for_update(
                analysis.service_id, analysis.requested_by
            )
            repository = ServiceAnalysisRepository(session)
            row = await repository.find_running(analysis.id, token, for_update=True)
            if row is None:
                return
            latest = await repository.find_latest(analysis.service_id)
            installations = await GithubInstallationRepository(session).search_by_user_id(
                analysis.requested_by
            )
            has_access = service is not None and any(
                item.id == service.github_installation_id
                and item.installation_id == analysis.github_installation_id
                for item in installations
            )
            if (
                service is None
                or source_context(service) != analysis_context(analysis)
                or latest is None
                or latest.id != analysis.id
                or not has_access
            ):
                row.status = AnalysisJobStatus.FAILED
                row.error_code = "ANALYSIS_STALE"
                row.stage = "failed"
            else:
                self._store_output(row, output)
                row.status = AnalysisJobStatus.SUCCEEDED
                row.stage = "complete"
            row.lease_token = None
            row.locked_until = None

    @staticmethod
    def _store_output(row: ServiceAnalysis, output: dict[str, JsonValue]) -> None:
        if (
            output.get("sourceSha") != row.source_sha
            or output.get("rootDirectory") != row.root_directory
            or output.get("deploymentAuthorized") is not False
        ):
            raise InvalidAnalyzerResultError("analyzer output changed the fixed source identity")
        for field, key in (
            ("source_snapshot_id", "sourceSnapshotId"),
            ("context_hash", "contextHash"),
            ("result_digest", "resultDigest"),
            ("analysis_status", "analysisStatus"),
        ):
            value = output.get(key)
            if not isinstance(value, str) or not value or len(value) > 128:
                raise InvalidAnalyzerResultError("invalid analyzer identity")
            setattr(row, field, value)
        for field, key in (
            ("analysis_result", "analysisResult"),
            ("verification_report", "verificationReport"),
            ("source_readiness", "sourceReadiness"),
            ("deployment_dossier", "deploymentDossier"),
        ):
            value = output.get(key)
            if not isinstance(value, dict):
                raise InvalidAnalyzerResultError("missing verified analyzer output")
            setattr(row, field, value)
        assert row.analysis_result is not None and row.verification_report is not None
        expected_digest = hashlib.sha256(
            json.dumps(
                row.analysis_result,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        ).hexdigest()
        if (
            row.analysis_result.get("sourceSnapshotId") != row.source_snapshot_id
            or row.analysis_result.get("contextHash") != row.context_hash
            or row.verification_report.get("sourceSnapshotId") != row.source_snapshot_id
            or row.verification_report.get("contextHash") != row.context_hash
            or row.verification_report.get("resultDigest") != row.result_digest
            or row.verification_report.get("deploymentAuthorized") is not False
            or row.result_digest != expected_digest
            or row.analysis_status not in {"complete", "needs_input", "unsupported"}
            or row.analysis_result.get("status") != row.analysis_status
            or row.verification_report.get("schemaVersion") != "iris.analysis-verification.v1"
            or not re.fullmatch(r"[a-f0-9]{64}", row.source_snapshot_id or "")
            or not re.fullmatch(r"[a-f0-9]{64}", row.context_hash or "")
        ):
            raise InvalidAnalyzerResultError("verification changed the analysis provenance")
        assert row.source_readiness is not None and row.deployment_dossier is not None
        source_link = row.deployment_dossier.get("sourceLink", {})
        if (
            row.source_readiness.get("sourceSnapshotId") != row.source_snapshot_id
            or not isinstance(source_link, dict)
            or source_link.get("sourceSnapshotId") != row.source_snapshot_id
            or source_link.get("analysisDigest") != row.result_digest
            or source_link.get("analysisContextHash") != row.context_hash
            or source_link.get("readinessContextHash") != row.source_readiness.get("contextHash")
        ):
            raise InvalidAnalyzerResultError("planning changed the verified analysis source")
        run_report = output.get("runReport")
        row.run_report = run_report if isinstance(run_report, dict) else None
        evidence = output.get("evidence")
        row.evidence = (
            [item for item in evidence if isinstance(item, dict)]
            if isinstance(evidence, list)
            else None
        )
        recommendation = output.get("builderRecommendation")
        row.builder_recommendation = recommendation if isinstance(recommendation, str) else None
        row.review_required = True

    async def _fail(self, analysis_id: str, token: str, code: str) -> None:
        async with self._session_factory.begin() as session:
            row = await ServiceAnalysisRepository(session).find_running(
                analysis_id, token, for_update=True
            )
            if row is None:
                return
            row.status = AnalysisJobStatus.FAILED
            row.stage = "failed"
            row.error_code = code[:64]
            row.lease_token = None
            row.locked_until = None

    async def _requeue(self, analysis_id: str, token: str) -> None:
        async with self._session_factory.begin() as session:
            row = await ServiceAnalysisRepository(session).find_running(
                analysis_id, token, for_update=True
            )
            if row is not None:
                row.status = AnalysisJobStatus.QUEUED
                row.stage = "queued"
                row.lease_token = None
                row.locked_until = None
