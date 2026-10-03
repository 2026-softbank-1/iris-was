"""Durable candidate orchestration with one submission and read-only uncertain recovery."""

import asyncio
import copy
import hashlib
import json
import logging
import re
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.aws_clients import SnapshotUrlClient
from app.clients.repair_agent_client import ARTIFACT_NAMES, RepairAgentClient, RepairAgentError
from app.core.exceptions import (
    AppError,
    ConflictError,
    DeploymentRequestNotFoundError,
    InvalidInputError,
    NotConfiguredError,
    NotFoundError,
    ServiceNotFoundError,
)
from app.enums import DiagnosisStatus
from app.models.base import now_utc
from app.models.deployment_diagnosis import DeploymentDiagnosis
from app.models.deployment_repair import DeploymentRepair
from app.models.deployment_request import DeploymentRequest
from app.models.service import Service
from app.repositories.build_repository import BuildRepository
from app.repositories.deployment_diagnosis_repository import DeploymentDiagnosisRepository
from app.repositories.deployment_repair_repository import DeploymentRepairRepository
from app.repositories.deployment_request_repository import DeploymentRequestRepository
from app.repositories.service_repository import ServiceRepository
from app.services.diagnosis_service import DIAGNOSABLE_STATUSES
from app.services.repair_handoff_service import RepairHandoffService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StartedRepair:
    repair: DeploymentRepair
    is_started: bool


RepairServiceOpener = Callable[[], AbstractAsyncContextManager["RepairService"]]


def _digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    ).hexdigest()


class RepairService:
    def __init__(
        self,
        session: AsyncSession,
        service_repository: ServiceRepository,
        deployment_repository: DeploymentRequestRepository,
        build_repository: BuildRepository,
        diagnosis_repository: DeploymentDiagnosisRepository,
        repair_repository: DeploymentRepairRepository,
        agent_client: RepairAgentClient | None,
        handoff_service: RepairHandoffService | None,
        snapshot_client: SnapshotUrlClient | None,
        *,
        max_cost_usd: float = 1,
        deadline_seconds: float = 240,
    ) -> None:
        self._session = session
        self._services = service_repository
        self._deployments = deployment_repository
        self._builds = build_repository
        self._diagnoses = diagnosis_repository
        self._repairs = repair_repository
        self._agent = agent_client
        self._handoff = handoff_service
        self._snapshots = snapshot_client
        self._max_cost_usd = max_cost_usd
        self._deadline_seconds = deadline_seconds

    async def _get_owned_service(self, owner_id: int, service_id: int) -> Service:
        service = await self._services.find_by_id_and_owner_id(service_id, owner_id)
        if service is None:
            raise ServiceNotFoundError("service not found", service_id=service_id)
        return service

    async def start_repair(
        self,
        owner_id: int,
        service_id: int,
        deployment_id: int,
        diagnosis_id: int,
        plan_ids: list[str],
        idempotency_key: str,
        *,
        auto_merge: bool = False,
        await_diagnosis: bool = False,
        owns_diagnosis: bool = False,
        configuration_keys: list[str] | None = None,
    ) -> StartedRepair:
        service = await self._get_owned_service(owner_id, service_id)
        request = await self._deployments.find_by_id_and_service_id(deployment_id, service_id)
        if request is None:
            raise DeploymentRequestNotFoundError(
                "deployment request not found", deployment_request_id=deployment_id
            )
        diagnosis = await self._diagnoses.get_by_id(diagnosis_id)
        if await_diagnosis:
            if (
                not auto_merge
                or diagnosis.deployment_request_id != request.id
                or request.status not in DIAGNOSABLE_STATUSES
            ):
                raise ConflictError("invalid pending diagnosis for repair")
        else:
            self._validate_selection(request, diagnosis, plan_ids)
        # Existing client keys retain their frozen source/policy even if server settings changed.
        existing = await self._repairs.find_by_service_id_and_key(service_id, idempotency_key)
        if existing is not None:
            if (
                existing.deployment_request_id != deployment_id
                or existing.diagnosis_id != diagnosis_id
                or existing.plan_ids != plan_ids
                or existing.diagnosis_result != diagnosis.result
                or bool(existing.request_metadata.get("autoMerge")) != auto_merge
            ):
                raise ConflictError("repair idempotency input changed", repair_id=existing.id)
            return StartedRepair(existing, False)
        if self._agent is None or self._handoff is None or self._snapshots is None:
            raise NotConfiguredError("repair agent or source snapshot is not configured")
        if not re.fullmatch(r"[0-9a-f]{40}", request.source_sha):
            raise InvalidInputError("repair requires a frozen 40-character source SHA")
        build = await self._builds.find_by_deployment_request_id(request.id)
        if (
            build is None or build.codebuild_build_id is None
        ) and request.source_deployment_request_id is not None:
            build = await self._builds.find_by_deployment_request_id(
                request.source_deployment_request_id
            )
        if (
            build is None
            or build.codebuild_build_id is None
            or now_utc() - build.created_at > timedelta(hours=23)
        ):
            raise ConflictError(
                "repair source snapshot is unavailable", deployment_request_id=request.id
            )
        if build.source_sha is not None and build.source_sha != request.source_sha:
            raise ConflictError("repair snapshot does not match deployment source")
        root = service.root_directory or "."
        policy = {
            "allowedPaths": ["*"] if root == "." else [root + "/**"],
            "protectedPaths": [],
            "maxCostUsd": self._max_cost_usd,
            "maxChangedFiles": 5,
            "maxChangedBytes": 65536,
        }
        stable = {
            "serviceId": service.id,
            "deploymentId": request.id,
            "diagnosisId": diagnosis.id,
            "planIds": plan_ids,
            "sourceSha": request.source_sha,
            "repository": service.source_repository_url,
            "rootDirectory": root,
            "diagnosisResult": diagnosis.result,
            "snapshotBuildId": build.id,
            "policy": policy,
        }
        values = {
            "service_id": service.id,
            "deployment_request_id": request.id,
            "diagnosis_id": diagnosis.id,
            "requested_by": owner_id,
            "idempotency_key": idempotency_key,
            "input_digest": _digest(stable),
            "source_sha": request.source_sha,
            "source_repository_url": service.source_repository_url,
            "root_directory": root,
            "plan_ids": plan_ids,
            "diagnosis_result": copy.deepcopy(diagnosis.result or {}),
            "request_metadata": {
                "snapshotBuildId": build.id,
                "policy": policy,
                **(
                    {
                        "autoMerge": True,
                        "autoRedeploy": True,
                        "awaitingDiagnosis": await_diagnosis,
                        "ownsDiagnosis": owns_diagnosis,
                        **(
                            {"strategy": "variables", "configurationKeys": configuration_keys}
                            if configuration_keys
                            else {}
                        ),
                        "autoDeadlineAt": (now_utc() + timedelta(minutes=30)).isoformat(),
                        "publication": {"status": "DIAGNOSING" if await_diagnosis else "QUEUED"},
                    }
                    if auto_merge
                    else {}
                ),
            },
            "deadline_at": now_utc() + timedelta(seconds=self._deadline_seconds),
        }
        repair = await self._repairs.add_running_if_absent(values)
        if repair is None:
            concurrent = await self._repairs.find_by_service_id_and_key(service_id, idempotency_key)
            if (
                concurrent is not None
                and concurrent.deployment_request_id == deployment_id
                and concurrent.diagnosis_id == diagnosis_id
                and concurrent.plan_ids == plan_ids
                and bool(concurrent.request_metadata.get("autoMerge")) == auto_merge
            ):
                return StartedRepair(concurrent, False)
            raise ConflictError(
                "repair is already running or idempotency input changed",
                deployment_request_id=request.id,
            )
        await self._session.commit()
        return StartedRepair(repair, True)

    @staticmethod
    def _validate_selection(
        request: DeploymentRequest, diagnosis: DeploymentDiagnosis, plan_ids: list[str]
    ) -> None:
        if request.status not in DIAGNOSABLE_STATUSES:
            raise ConflictError("only failed deployments can be repaired")
        raw = diagnosis.result
        if (
            diagnosis.deployment_request_id != request.id
            or diagnosis.status != DiagnosisStatus.SUCCEEDED
            or not isinstance(raw, dict)
            or raw.get("schema_version") != "diagnosis-result.v3"
            or raw.get("job_status") != "succeeded"
        ):
            raise InvalidInputError(
                "repair diagnosis does not match this successful deployment diagnosis"
            )
        remediation = (raw.get("analysis") or {}).get("remediation")
        plans = remediation.get("plans") if isinstance(remediation, dict) else None
        if (
            not isinstance(plans, list)
            or any(not isinstance(plan, dict) for plan in plans)
            or not set(plan_ids) <= {plan.get("id") for plan in plans}
        ):
            raise InvalidInputError("selected plans do not belong to diagnosis")

    async def run_repair(self, owner_id: int, service_id: int, repair_id: int) -> DeploymentRepair:
        await self._get_owned_service(owner_id, service_id)
        repair = await self._repairs.get_by_id(repair_id)
        if repair.service_id != service_id:
            raise NotFoundError("repair not found", repair_id=repair_id)
        if repair.request_metadata.get("autoMerge") and repair.generation_started_at is None:
            # Queue time is separate from the model timeout; set it once with the generation claim.
            deadline = min(
                now_utc() + timedelta(seconds=self._deadline_seconds),
                datetime.fromisoformat(repair.request_metadata["autoDeadlineAt"]),
            )
            claimed = await self._repairs.claim_generation(repair_id, deadline_at=deadline)
            if claimed:
                repair.deadline_at = deadline
        else:
            claimed = await self._repairs.claim_generation(repair_id)
        if not claimed:
            return repair
        await self._session.commit()
        assert self._agent is not None and self._handoff is not None and self._snapshots is not None
        submitted = False
        try:
            # Construct transient frozen inputs; current Service changes cannot alter this attempt.
            frozen_service = Service(
                id=service_id,
                source_repository_url=repair.source_repository_url,
                root_directory=repair.root_directory,
            )
            frozen_deployment = DeploymentRequest(
                id=repair.deployment_request_id,
                service_id=service_id,
                source_sha=repair.source_sha,
                status="FAILED",
            )
            frozen_diagnosis = DeploymentDiagnosis(
                id=repair.diagnosis_id,
                deployment_request_id=repair.deployment_request_id,
                status=DiagnosisStatus.SUCCEEDED,
                result=repair.diagnosis_result,
            )
            await self._session.commit()
            remaining = (repair.deadline_at - now_utc()).total_seconds()
            if remaining <= 0:
                raise InvalidInputError("repair deadline expired")
            async with asyncio.timeout(remaining):
                url = await self._snapshots.presign_snapshot(
                    repair.request_metadata["snapshotBuildId"]
                )
                policy = repair.request_metadata["policy"]
                payload = await self._handoff.prepare_request(
                    frozen_service,
                    frozen_deployment,
                    frozen_diagnosis,
                    request_id=repair.agent_request_id,
                    plan_ids=repair.plan_ids,
                    download_url=url,
                    allowed_paths=policy["allowedPaths"],
                    protected_paths=policy["protectedPaths"],
                    deadline=repair.deadline_at,
                    max_cost_usd=policy["maxCostUsd"],
                    max_changed_files=policy["maxChangedFiles"],
                    max_changed_bytes=policy["maxChangedBytes"],
                )
                stable_payload = copy.deepcopy(payload)
                stable_payload["source"].pop("downloadUrl")
                stable_payload["policy"]["deadline"] = repair.deadline_at.isoformat().replace(
                    "+00:00", "Z"
                )
                repair.request_metadata = {
                    **repair.request_metadata,
                    "agentInputDigest": _digest(stable_payload),
                    "archiveSha256": payload["source"]["archiveSha256"],
                    "manifestSha256": payload["source"]["manifestSha256"],
                }
                await self._session.commit()
                submitted = True
                result = await self._agent.submit(payload, repair.agent_request_id)
                self._apply_result(repair, result)
        except (TimeoutError, asyncio.CancelledError):
            repair.finish(
                "UNKNOWN_OUTCOME" if submitted else "FAILED",
                error_code="MODEL_CALL_UNKNOWN" if submitted else "DEADLINE_EXCEEDED",
            )
            await self._session.commit()
        except AppError as exc:
            code = str(exc.fields.get("agent_code") or exc.code)[:64]
            rejected = isinstance(exc, RepairAgentError) and exc.agent_status in {
                400,
                401,
                403,
                404,
                405,
                413,
                415,
                422,
                429,
            }
            repair.finish(
                "UNKNOWN_OUTCOME" if submitted and not rejected else "FAILED", error_code=code
            )
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            repair = await self._repairs.get_by_id(repair_id)
            repair.finish("UNKNOWN_OUTCOME" if submitted else "FAILED", error_code="INTERNAL_ERROR")
            await self._session.commit()
            raise
        else:
            await self._session.commit()
        return repair

    def _apply_result(self, repair: DeploymentRepair, result: dict[str, Any]) -> None:
        if result.get("status") == "SUCCEEDED" and isinstance(result.get("result"), dict):
            if result.get("requestId") != repair.agent_request_id:
                raise RepairAgentError(
                    "repair receipt scope mismatch", agent_code="INVALID_RESPONSE"
                )
            result = result["result"]
        RepairHandoffService.validate_result(
            result, request_id=repair.agent_request_id, base_commit_sha=repair.source_sha
        )
        expected = repair.request_metadata.get("agentInputDigest")
        if expected is not None and result.get("inputDigest") != expected:
            raise RepairAgentError("repair input digest mismatch", agent_code="INVALID_RESPONSE")
        status = result.get("status")
        if status in {"RUNNING", "UNKNOWN_OUTCOME"}:
            repair.finish("UNKNOWN_OUTCOME", result=result, error_code="MODEL_CALL_UNKNOWN")
        elif status == "FAILED":
            repair.finish(
                "FAILED",
                result=result.get("result"),
                error_code=str(result.get("errorCode") or "REPAIR_AGENT_ERROR")[:64],
            )
        else:
            # Public artifact URLs must route through ownership checks, not the internal agent.
            public_result = copy.deepcopy(result)
            for artifact in public_result.get("artifacts", []):
                artifact["url"] = (
                    f"/api/v1/services/{repair.service_id}/repairs/{repair.id}/artifacts/{artifact['name']}"
                )
            repair.finish("SUCCEEDED", result=public_result)

    async def get_repair(self, owner_id: int, service_id: int, repair_id: int) -> DeploymentRepair:
        await self._get_owned_service(owner_id, service_id)
        repair = await self._repairs.get_by_id(repair_id)
        if repair.service_id != service_id:
            raise NotFoundError("repair not found", repair_id=repair_id)
        if repair.status == "UNKNOWN_OUTCOME" or (
            repair.status == "RUNNING"
            and not (
                repair.request_metadata.get("autoMerge") and repair.generation_started_at is None
            )
            and now_utc() > repair.deadline_at + timedelta(seconds=10)
        ):
            await self._session.commit()
            if self._agent is not None:
                try:
                    receipt = await self._agent.get_receipt(repair.agent_request_id)
                    self._apply_result(repair, receipt)
                except AppError:
                    repair.finish("UNKNOWN_OUTCOME", error_code="MODEL_CALL_UNKNOWN")
                await self._session.commit()
            elif repair.status == "RUNNING":
                repair.finish("UNKNOWN_OUTCOME", error_code="MODEL_CALL_UNKNOWN")
                await self._session.commit()
        return repair

    async def latest_repair(
        self, owner_id: int, service_id: int, deployment_id: int, diagnosis_id: int
    ) -> DeploymentRepair:
        await self._get_owned_service(owner_id, service_id)
        repair = await self._repairs.latest(service_id, deployment_id, diagnosis_id)
        return await self.get_repair(owner_id, service_id, repair.id)

    async def get_artifact(
        self, owner_id: int, service_id: int, repair_id: int, name: str
    ) -> bytes:
        repair = await self.get_repair(owner_id, service_id, repair_id)
        if name not in ARTIFACT_NAMES or repair.status != "SUCCEEDED" or repair.result is None:
            raise NotFoundError("repair artifact not found", repair_id=repair_id)
        artifact = next(
            (item for item in repair.result.get("artifacts", []) if item["name"] == name), None
        )
        if artifact is None:
            raise NotFoundError("repair artifact not found", repair_id=repair_id)
        if self._handoff is None:
            raise NotConfiguredError("repair agent is not configured")
        await self._session.commit()
        return await self._handoff.get_verified_artifact(repair.agent_request_id, name, artifact)


async def run_repair_in_background(
    open_service: RepairServiceOpener, owner_id: int, service_id: int, repair_id: int
) -> None:
    try:
        async with open_service() as service:
            await service.run_repair(owner_id, service_id, repair_id)
    except AppError:
        return
    except Exception:
        logger.exception(
            "repair crashed",
            extra={
                "action": "run_repair_in_background",
                "service_id": service_id,
                "repair_id": repair_id,
            },
        )
