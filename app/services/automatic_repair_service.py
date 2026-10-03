"""A user click authorizes generation, hotfix publication and merge without another prompt."""

import asyncio
import contextlib
import copy
import logging
import re
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    AppError,
    ConflictError,
    DiagnosisInProgressError,
    FieldIssue,
    InvalidInputError,
    NotConfiguredError,
)
from app.enums import DeploymentTrigger, DiagnosisStatus
from app.models.base import now_utc
from app.models.deployment_repair import DeploymentRepair
from app.repositories.deployment_repair_repository import DeploymentRepairRepository
from app.services.deployment_request_service import DeploymentRequestService
from app.services.diagnosis_service import DiagnosisService
from app.services.repair_publication_service import RepairPublicationService
from app.services.repair_service import RepairService, StartedRepair, _digest

logger = logging.getLogger(__name__)


def environment_variable_names(result: dict[str, Any]) -> list[str]:
    plans = ((result.get("analysis") or {}).get("remediation") or {}).get("plans", [])
    names = set(
        {
            c["target"]
            for p in plans
            for c in (p.get("changes") or [])
            if c.get("kind") == "configuration"
            and isinstance(c.get("target"), str)
            and re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", c["target"])
        }
    )
    evidence = "\n".join(
        e.get("text", "") for e in result.get("evidence", []) if isinstance(e, dict)
    )
    names.update(
        re.findall(
            r"[\"']path[\"']\s*:\s*\[\s*[\"']([A-Z][A-Z0-9_]{0,127})[\"']\s*\]"
            r"[\s\S]{0,250}?received undefined",
            evidence,
        )
    )
    return sorted(names)


class ConfigurationRequiredError(InvalidInputError):
    code = "CONFIGURATION_VALUES_REQUIRED"
    status_code = 409

    def __init__(self, keys: list[str]) -> None:
        super().__init__(
            "Add or upload environment variables, then redeploy the service",
            issues=[
                FieldIssue(field=key, reason="developer_configuration_required") for key in keys
            ],
        )


class AutomaticRepairService:
    def __init__(
        self,
        session: AsyncSession,
        repairs: DeploymentRepairRepository,
        candidates: RepairService,
        publication: RepairPublicationService,
        diagnostics: DiagnosisService | None = None,
        deployer: DeploymentRequestService | None = None,
    ) -> None:
        self._session = session
        self._repairs = repairs
        self._candidates = candidates
        self._publication = publication
        self._diagnostics = diagnostics
        self._deployer = deployer

    async def start(
        self, owner_id: int, service_id: int, deployment_id: int, diagnosis_id: int | None, key: str
    ) -> StartedRepair:
        await self._candidates._get_owned_service(owner_id, service_id)
        existing = await self._repairs.find_by_service_id_and_key(service_id, key)
        if existing is not None:
            if (
                existing.deployment_request_id != deployment_id
                or (diagnosis_id is not None and existing.diagnosis_id != diagnosis_id)
                or not existing.request_metadata.get("autoMerge")
            ):
                raise ConflictError("automatic repair idempotency input changed")
            return StartedRepair(existing, False)
        request = await self._candidates._deployments.find_by_id_and_service_id(
            deployment_id, service_id
        )
        if request is None:
            raise ConflictError("deployment not found")
        owns_diagnosis = False
        if diagnosis_id is None:
            if self._diagnostics is None:
                raise NotConfiguredError("diagnosis service is not configured")
            try:
                started = await self._diagnostics.start_diagnosis(
                    owner_id, service_id, deployment_id
                )
                diagnosis = started.diagnosis
                owns_diagnosis = started.is_started
            except DiagnosisInProgressError:
                latest = await self._candidates._diagnoses.find_latest_by_deployment_request_id(
                    deployment_id
                )
                if latest is None:
                    raise ConflictError("diagnosis was not found") from None
                diagnosis = latest
        else:
            diagnosis = await self._candidates._diagnoses.get_by_id(diagnosis_id)
        awaiting = diagnosis.status == DiagnosisStatus.RUNNING
        raw = diagnosis.result or {}
        plans = ((raw.get("analysis") or {}).get("remediation") or {}).get("plans", [])
        plan_ids = [
            p["id"]
            for p in plans
            if p.get("changes") and all(c.get("kind") == "code" for c in p["changes"])
        ]
        if not awaiting:
            self._candidates._validate_selection(request, diagnosis, plan_ids)
            keys = environment_variable_names(raw)
            if keys or not plan_ids:
                raise ConfigurationRequiredError(keys)
            await self._publication.preflight(owner_id, service_id, request.source_sha)
        created = await self._candidates.start_repair(
            owner_id,
            service_id,
            deployment_id,
            diagnosis.id,
            plan_ids,
            key,
            auto_merge=True,
            await_diagnosis=awaiting,
            owns_diagnosis=owns_diagnosis,
        )
        return created

    async def _require_developer_configuration(self, repair: DeploymentRepair) -> None:
        repair = await self._repairs.lock_publication(repair.id)
        keys = environment_variable_names(repair.diagnosis_result)
        if not keys:
            keys = [
                k
                for k in repair.request_metadata.get("configurationKeys", [])
                if isinstance(k, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", k)
            ]
        repair.finish(
            "SUCCEEDED", result={"status": "configuration_required", "variableNames": keys}
        )
        repair.request_metadata = {**repair.request_metadata, "publication": {"status": "SKIPPED"}}
        await self._session.commit()

    async def _await_diagnosis(
        self, owner_id: int, service_id: int, repair: DeploymentRepair
    ) -> bool:
        diagnosis = await self._candidates._diagnoses.get_by_id(repair.diagnosis_id)
        if (
            diagnosis.status == DiagnosisStatus.RUNNING
            and repair.request_metadata.get("ownsDiagnosis")
            and not repair.request_metadata.get("diagnosisStarted")
        ):
            repair = await self._repairs.lock_publication(repair.id)
            if not repair.request_metadata.get("diagnosisStarted"):
                repair.request_metadata = {**repair.request_metadata, "diagnosisStarted": True}
                await self._session.commit()
                assert self._diagnostics is not None
                try:
                    await self._diagnostics.run_diagnosis(
                        owner_id, service_id, repair.deployment_request_id, diagnosis.id
                    )
                except AppError:
                    pass
            else:
                await self._session.commit()
            diagnosis = await self._candidates._diagnoses.get_by_id(repair.diagnosis_id)
        if diagnosis.status == DiagnosisStatus.RUNNING:
            if now_utc() - diagnosis.created_at > timedelta(minutes=4):
                repair.finish("FAILED", error_code="DIAGNOSIS_ABANDONED")
                await self._session.commit()
            return False
        if diagnosis.status != DiagnosisStatus.SUCCEEDED:
            repair.finish("FAILED", error_code=diagnosis.error_code or "DIAGNOSIS_FAILED")
            await self._session.commit()
            return False
        raw = diagnosis.result or {}
        plans = ((raw.get("analysis") or {}).get("remediation") or {}).get("plans", [])
        ids = [
            p["id"]
            for p in plans
            if p.get("changes") and all(c.get("kind") == "code" for c in p["changes"])
        ]
        if environment_variable_names(raw) or not ids:
            repair.diagnosis_result = copy.deepcopy(raw)
            await self._require_developer_configuration(repair)
            return False
        await self._publication.preflight(owner_id, service_id, repair.source_sha)
        request = await self._candidates._deployments.find_by_id_and_service_id(
            repair.deployment_request_id, service_id
        )
        if request is None:
            raise ConflictError("deployment not found")
        self._candidates._validate_selection(request, diagnosis, ids)
        repair = await self._repairs.lock_publication(repair.id)
        if repair.request_metadata.get("awaitingDiagnosis"):
            repair.plan_ids = ids
            repair.diagnosis_result = copy.deepcopy(raw)
            repair.input_digest = _digest(
                {
                    "sourceSha": repair.source_sha,
                    "diagnosis": raw,
                    "planIds": ids,
                    "policy": repair.request_metadata["policy"],
                }
            )
            repair.request_metadata = {
                **repair.request_metadata,
                "awaitingDiagnosis": False,
                "publication": {"status": "QUEUED"},
            }
        await self._session.commit()
        return True

    async def _redeploy(self, owner_id: int, service_id: int, repair: DeploymentRepair) -> None:
        if self._deployer is None:
            return
        repair = await self._repairs.lock_publication(repair.id)
        publication = dict(repair.request_metadata.get("publication") or {})
        if publication.get("redeploymentId"):
            await self._session.commit()
            return
        service = await self._candidates._get_owned_service(owner_id, service_id)
        sha = publication["mergeCommitSha"]
        deployment = await self._candidates._deployments.find_by_idempotency_key(
            f"auto-repair-redeploy:{repair.id}"
        )
        if deployment is None:
            await self._publication.preflight(owner_id, service_id, sha)
            deployment = await self._deployer.create_deployment_request(
                service,
                source_sha=sha,
                source_commit_message=f"AI repair #{repair.id}",
                trigger_type=DeploymentTrigger.MANUAL,
                idempotency_key=f"auto-repair-redeploy:{repair.id}",
                requested_by=owner_id,
            )
        if deployment is not None:
            publication["redeploymentId"] = deployment.id
            repair.request_metadata = {**repair.request_metadata, "publication": publication}
        await self._session.commit()

    async def resume(self, owner_id: int, service_id: int, repair_id: int) -> DeploymentRepair:
        repair = await self._candidates.get_repair(owner_id, service_id, repair_id)
        if (repair.request_metadata.get("publication") or {}).get("status") == "MERGED":
            return repair
        if repair.status == "FAILED":
            raise ConflictError("generation failed; start a new automatic repair attempt")
        if repair.request_metadata.get("strategy") == "variables":
            await self._require_developer_configuration(repair)
            return repair
        await self._publication.preflight(owner_id, service_id)
        repair = await self._repairs.lock_publication(repair_id)
        metadata = dict(repair.request_metadata)
        publication = dict(metadata.get("publication") or {})
        if publication.get("status") != "MERGED":
            publication.update(status="QUEUED")
            publication.pop("errorCode", None)
            metadata.update(
                autoMerge=True,
                autoRedeploy=True,
                autoDeadlineAt=(now_utc() + timedelta(minutes=30)).isoformat(),
                publication=publication,
            )
            repair.request_metadata = metadata
            await self._session.commit()
        return repair

    async def advance(self, owner_id: int, service_id: int, repair_id: int) -> None:
        repair = await self._candidates.get_repair(owner_id, service_id, repair_id)
        if not repair.request_metadata.get("autoMerge"):
            return  # An old candidate or opening the page never grants merge authority.
        publication = repair.request_metadata.get("publication") or {}
        if publication.get("status") == "MERGED":
            if repair.request_metadata.get("autoRedeploy") and not publication.get(
                "redeploymentId"
            ):
                await self._redeploy(owner_id, service_id, repair)
            return
        if publication.get("status") in {"ERROR", "SKIPPED", "REDEPLOY_REQUESTED"}:
            return
        if datetime.fromisoformat(repair.request_metadata["autoDeadlineAt"]) < now_utc():
            repair = await self._repairs.lock_publication(repair_id)
            publication = repair.request_metadata.get("publication") or {}
            # A concurrent completion or explicit retry may have changed the row while we waited.
            if publication.get("status") not in {"MERGED", "ERROR", "SKIPPED"} and (
                datetime.fromisoformat(repair.request_metadata["autoDeadlineAt"]) < now_utc()
            ):
                repair.request_metadata = {
                    **repair.request_metadata,
                    "publication": {
                        **publication,
                        "status": "ERROR",
                        "errorCode": "DEADLINE_EXCEEDED",
                    },
                }
                if repair.status == "RUNNING" and repair.generation_started_at is None:
                    repair.finish("FAILED", error_code="DEADLINE_EXCEEDED")
            await self._session.commit()
            return
        if repair.request_metadata.get("awaitingDiagnosis"):
            if not await self._await_diagnosis(owner_id, service_id, repair):
                return
        if repair.request_metadata.get("strategy") == "variables":
            await self._require_developer_configuration(repair)
            return
        if repair.status == "RUNNING":
            repair = await self._candidates.run_repair(owner_id, service_id, repair_id)
        if repair.status != "SUCCEEDED":
            return
        if (repair.result or {}).get("status") != "candidate_ready":
            repair.request_metadata = {
                **repair.request_metadata,
                "publication": {"status": "SKIPPED"},
            }
            await self._session.commit()
            return
        try:
            if not (repair.request_metadata.get("publication") or {}).get("pullUrl"):
                repair = await self._publication.execute(owner_id, service_id, repair_id, "publish")
            repair = await self._publication.execute(owner_id, service_id, repair_id, "merge")
            if repair.request_metadata.get("autoRedeploy"):
                await self._redeploy(owner_id, service_id, repair)
        except AppError as exc:
            if exc.fields.get("publication_busy"):
                return
            try:
                repair = await self._repairs.lock_publication(repair_id)
            except ConflictError as locked:
                if locked.fields.get("publication_busy"):
                    return
                raise
            publication = dict(repair.request_metadata.get("publication") or {})
            if publication.get("status") in {"MERGED", "SKIPPED"} or (
                publication.get("status") == "ERROR"
                and publication.get("errorCode") not in {exc.code, "GITHUB_OUTCOME_UNKNOWN"}
            ):
                await self._session.commit()
                return
            if exc.code == "MERGE_BLOCKED":
                # Required CI can finish later; retry this same PR without another model call.
                publication.update(status="WAITING_CHECKS", errorCode="MERGE_BLOCKED")
            elif publication.get("errorCode") == "GITHUB_OUTCOME_UNKNOWN":
                publication.update(status="RECOVERING")
            else:
                publication.update(status="ERROR", errorCode=exc.code)
            repair.request_metadata = {**repair.request_metadata, "publication": publication}
            await self._session.commit()

    async def pending(self) -> list[tuple[int, int, int]]:
        return [
            (r.requested_by, r.service_id, r.id)
            for r in await self._repairs.pending_automatic()
            if r.requested_by is not None
        ]


AutomaticRepairOpener = Callable[[], AbstractAsyncContextManager[AutomaticRepairService]]


class AutomaticRepairRunner:
    def __init__(self, open_service: AutomaticRepairOpener, interval_seconds: float = 5) -> None:
        self._open_service = open_service
        self._interval = interval_seconds
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self.run(), name="automatic-repair")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._task, timeout=20)
            self._task = None

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                async with self._open_service() as service:
                    jobs = await service.pending()
                for owner_id, service_id, repair_id in jobs:
                    if self._stop.is_set():
                        break
                    try:
                        async with self._open_service() as service:
                            await service.advance(owner_id, service_id, repair_id)
                    except AppError:
                        continue
            except Exception:
                logger.exception("automatic repair iteration failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
