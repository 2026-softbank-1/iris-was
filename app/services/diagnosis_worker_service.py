"""Lease-owned log diagnosis; deployment status remains an immutable input."""

import asyncio
import json
import logging
import re
from typing import Any

from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.diagnosis_client import DiagnosisClient
from app.clients.failure_log_client import FailureLogClient, _tail
from app.core.diagnosis_config import DiagnosisSettings
from app.core.exceptions import AppError
from app.enums import DiagnosisJobStatus
from app.models.deployment_diagnosis import DeploymentDiagnosis
from app.models.deployment_request import DeploymentRequest
from app.repositories.diagnosis_repository import DiagnosisRepository
from app.repositories.service_repository import ServiceRepository

logger = logging.getLogger(__name__)


class DiagnosisWorkerService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        log_client: FailureLogClient,
        diagnosis_client: DiagnosisClient,
        settings: DiagnosisSettings,
    ) -> None:
        self._session_factory = session_factory
        self._log_client = log_client
        self._diagnosis_client = diagnosis_client
        self._settings = settings

    async def claim_next_diagnosis(self) -> DeploymentDiagnosis | None:
        async with self._session_factory.begin() as session:
            return await DiagnosisRepository(session).claim_next(self._settings.lease_seconds)

    async def process_diagnosis(self, row: DeploymentDiagnosis) -> None:
        token = row.lease_token
        assert token is not None
        task = asyncio.create_task(self._execute(row, token))
        heartbeat = asyncio.create_task(self._heartbeat(row.id, token, task))
        try:
            await task
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            # A remote request may still run. Never automatically dispatch another paid inference.
            await self._finish(row.id, token, DiagnosisJobStatus.FAILED, "DIAGNOSIS_INTERRUPTED")
        except TimeoutError:
            await self._finish(row.id, token, DiagnosisJobStatus.TIMED_OUT, "DIAGNOSIS_TIMED_OUT")
        except AppError as error:
            reason = error.fields.get("reason", error.code)
            await self._finish(row.id, token, DiagnosisJobStatus.FAILED, str(reason))
        except Exception:
            logger.error(
                "diagnosis attempt failed", extra={"action": "diagnose", "diagnosis_id": row.id}
            )
            await self._finish(row.id, token, DiagnosisJobStatus.FAILED, "DIAGNOSIS_FAILED")
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _heartbeat(self, row_id: str, token: str, task: asyncio.Task[None]) -> None:
        try:
            while not task.done():
                await asyncio.sleep(self._settings.lease_seconds / 3)
                async with self._session_factory.begin() as session:
                    retained = await DiagnosisRepository(session).renew_lease(
                        row_id, token, self._settings.lease_seconds
                    )
                if not retained:
                    task.cancel()
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            task.cancel()
            logger.error(
                "diagnosis lease renewal failed", extra={"action": "renew_diagnosis_lease"}
            )

    async def _execute(self, row: DeploymentDiagnosis, token: str) -> None:
        async with asyncio.timeout(self._settings.timeout_seconds + 45):
            if row.stage == "model_started":
                await self._finish(
                    row.id, token, DiagnosisJobStatus.FAILED, "DIAGNOSIS_RECOVERY_UNCERTAIN"
                )
                return
            if row.attempts > 3:
                await self._finish(
                    row.id, token, DiagnosisJobStatus.FAILED, "DIAGNOSIS_ATTEMPTS_EXHAUSTED"
                )
                return
            selection = self._settings.model_selection()
            if selection is None:
                await self._finish(row.id, token, DiagnosisJobStatus.FAILED, "MODEL_NOT_CONFIGURED")
                return
            if selection != row.model_selection:
                await self._finish(
                    row.id, token, DiagnosisJobStatus.FAILED, "MODEL_CONFIGURATION_CHANGED"
                )
                return
            async with self._session_factory() as session:
                service = await ServiceRepository(session).find_by_id_and_owner_id(
                    row.service_id, row.requested_by
                )
                deployment = (
                    await session.scalars(
                        select(DeploymentRequest).where(
                            DeploymentRequest.id == row.deployment_id,
                            DeploymentRequest.service_id == row.service_id,
                        )
                    )
                ).one_or_none()
                if service is None or deployment is None:
                    await self._finish(
                        row.id, token, DiagnosisJobStatus.FAILED, "DIAGNOSIS_ACCESS_REVOKED"
                    )
                    return
                project_id = service.project_id
                snapshot = row.source_logs
                if snapshot is None:
                    snapshot = await self._log_client.collect(session, deployment)
            if not snapshot.get("logs"):
                await self._store_snapshot(row.id, token, snapshot)
                await self._finish(
                    row.id, token, DiagnosisJobStatus.FAILED, "DIAGNOSIS_LOGS_UNAVAILABLE"
                )
                return
            request = {
                "schema_version": "diagnosis-request.v1",
                "tenant_id": f"user-{row.requested_by}",
                "project_id": f"project-{project_id}",
                "deployment_id": f"deployment-{row.deployment_id}",
                "attempt_id": row.attempt_id,
                "trigger": row.trigger,
                "context": row.deployment_context,
                "logs": snapshot["logs"],
                "model_profile_id": self._settings.profile_id,
                "model_settings_version": self._settings.settings_version(),
                "previous_diagnosis_id": None,
            }
            self._fit_snapshot(request, snapshot)
            await self._store_snapshot(row.id, token, snapshot, model_started=True)
            # No session, AWS credential, repository content or env value enters the model adapter.
            output = await self._diagnosis_client.diagnose(request)
            self._validate_output(request, output)
            limitations = output.get("input_limitations")
            output["input_limitations"] = list(
                dict.fromkeys(
                    [str(item) for item in (limitations if isinstance(limitations, list) else [])]
                    + [str(item) for item in snapshot.get("limitations", [])]
                )
            )
            output["log_sources"] = snapshot.get("sources", [])
            status = {
                "succeeded": DiagnosisJobStatus.SUCCEEDED,
                "failed": DiagnosisJobStatus.FAILED,
                "timed_out": DiagnosisJobStatus.TIMED_OUT,
            }[str(output["job_status"])]
            error = output.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            await self._finish(row.id, token, status, str(code) if code else None, output)

    def _fit_snapshot(self, request: dict[str, Any], snapshot: dict[str, Any]) -> None:
        """Account for evidence metadata overhead while keeping a physical tail mapping."""
        from ai_error_check_agent.contracts import DiagnosisRequest
        from ai_error_check_agent.errors import DiagnosisError
        from ai_error_check_agent.preprocessing import prepare

        while True:
            try:
                prepare(
                    DiagnosisRequest.model_validate_json(json.dumps(request)),
                    self._settings.max_evidence_bytes,
                )
                return
            except DiagnosisError as error:
                if error.code != "INPUT_TOO_LARGE":
                    raise
                logs = request["logs"]
                largest = max(logs, key=lambda chunk: len(chunk["text"].encode()))
                length = len(largest["text"].encode())
                if length <= 256:
                    raise
                largest["text"], _ = _tail(largest["text"], max(256, length * 3 // 4))
                largest["source_line_start"] = None
                largest["is_complete"] = False
                if not largest["text"].strip():
                    raise
                limit = (
                    "Log tail was reduced to fit the evidence JSON budget; "
                    "earlier lines may be missing."
                )
                if limit not in snapshot["limitations"]:
                    snapshot["limitations"].append(limit)

    @staticmethod
    def _validate_output(request: dict[str, Any], output: dict[str, JsonValue]) -> None:
        if (
            output.get("schema_version") != "diagnosis-result.v2"
            or output.get("remediation_execution") != "not_executed"
            or output.get("scope")
            != {
                key: request[key]
                for key in ("tenant_id", "project_id", "deployment_id", "attempt_id")
            }
            or output.get("deployment_context") != request["context"]
            or output.get("job_status") not in {"succeeded", "failed", "timed_out"}
        ):
            raise ValueError("diagnosis provenance mismatch")
        analysis = output.get("analysis")
        if output.get("job_status") == "succeeded" and (
            not isinstance(analysis, dict)
            or analysis.get("analysis_status")
            not in {"diagnosed", "insufficient_evidence", "no_failure_evidence"}
            or not isinstance(analysis.get("remediation"), dict)
        ):
            raise ValueError("diagnosis result missing v2 analysis")
        if output.get("job_status") != "succeeded" and analysis is not None:
            raise ValueError("failed diagnosis contains a manufactured analysis")

    async def _store_snapshot(
        self, row_id: str, token: str, snapshot: dict[str, Any], *, model_started: bool = False
    ) -> None:
        async with self._session_factory.begin() as session:
            row = await DiagnosisRepository(session).find_running(row_id, token, for_update=True)
            if row is None:
                raise asyncio.CancelledError
            row.source_logs = snapshot
            row.stage = "model_started" if model_started else "collecting_logs"

    async def _finish(
        self,
        row_id: str,
        token: str,
        status: DiagnosisJobStatus,
        code: str | None = None,
        output: dict[str, JsonValue] | None = None,
    ) -> None:
        async with self._session_factory.begin() as session:
            row = await DiagnosisRepository(session).find_running(row_id, token, for_update=True)
            if row is None:
                return
            # Recheck access before persisting user-visible output after a long model invocation.
            service = await ServiceRepository(session).find_by_id_and_owner_id(
                row.service_id, row.requested_by
            )
            if service is None:
                status, code, output = DiagnosisJobStatus.FAILED, "DIAGNOSIS_ACCESS_REVOKED", None
            row.status = status
            row.stage = "complete" if status == DiagnosisJobStatus.SUCCEEDED else "failed"
            row.error_code = re.sub(r"[^A-Z0-9_]", "", code or "")[:64] or None
            row.result = output
            row.lease_token = None
            row.locked_until = None
