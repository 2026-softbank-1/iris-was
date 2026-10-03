"""Create pinned repair requests from owned, persisted deployment diagnosis records."""

import re
from datetime import UTC, datetime
from typing import Any

from app.clients.repair_agent_client import (
    ARTIFACT_NAMES,
    RepairAgentClient,
    RepairAgentError,
    RepairSourceClient,
)
from app.core.exceptions import ConflictError, InvalidInputError
from app.enums import DiagnosisStatus
from app.models.deployment_diagnosis import DeploymentDiagnosis
from app.models.deployment_request import DeploymentRequest
from app.models.service import Service
from app.services.diagnosis_service import DIAGNOSABLE_STATUSES
from app.services.repair_source import sha256, validate_path
from app.services.repository_url import parse_repository_url


class RepairHandoffService:
    def __init__(self, agent_client: RepairAgentClient, source_client: RepairSourceClient) -> None:
        self._agent = agent_client
        self._source = source_client

    async def prepare_request(
        self,
        service: Service,
        deployment: DeploymentRequest,
        diagnosis: DeploymentDiagnosis,
        *,
        request_id: str,
        plan_ids: list[str],
        download_url: str,
        allowed_paths: list[str],
        protected_paths: list[str],
        deadline: datetime,
        max_cost_usd: float,
        max_changed_files: int = 5,
        max_changed_bytes: int = 65536,
    ) -> dict[str, Any]:
        """Caller checks ownership and resolves the existing build's exact snapshot URL."""
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", request_id):
            raise InvalidInputError("invalid repair request identifier")
        if deployment.service_id != service.id or diagnosis.deployment_request_id != deployment.id:
            raise InvalidInputError("repair scope does not match deployment")
        if deployment.status not in DIAGNOSABLE_STATUSES:
            raise ConflictError("only failed deployments can be repaired")
        raw = diagnosis.result
        if (
            diagnosis.status != DiagnosisStatus.SUCCEEDED
            or not isinstance(raw, dict)
            or raw.get("schema_version") != "diagnosis-result.v3"
            or raw.get("job_status") != "succeeded"
        ):
            raise ConflictError("repair requires an original successful diagnosis")
        analysis = raw.get("analysis")
        remediation = analysis.get("remediation") if isinstance(analysis, dict) else None
        plans = remediation.get("plans") if isinstance(remediation, dict) else None
        if (
            not isinstance(plans, list)
            or any(
                not isinstance(plan, dict) or not isinstance(plan.get("id"), str) for plan in plans
            )
            or not plan_ids
            or len(plan_ids) > 20
            or any(
                not isinstance(plan_id, str) or not plan_id or len(plan_id) > 128
                for plan_id in plan_ids
            )
            or len(set(plan_ids)) != len(plan_ids)
            or not set(plan_ids) <= {plan.get("id") for plan in plans}
        ):
            raise InvalidInputError("selected repair plans do not belong to diagnosis")
        if not re.fullmatch(r"[0-9a-f]{40}", deployment.source_sha):
            raise InvalidInputError("repair requires a frozen source commit")
        root = service.root_directory or "."
        validate_path(root, root=True)
        for scope_key in ("backend_context", "scope"):
            scope = raw.get(scope_key) or {}
            if not isinstance(scope, dict) or any(
                key in scope and str(scope[key]) != str(expected)
                for key, expected in (("service_id", service.id), ("deployment_id", deployment.id))
            ):
                raise InvalidInputError("diagnosis scope does not match repair")
        source_analysis = raw.get("source_analysis") or {}
        if (
            not isinstance(source_analysis, dict)
            or (
                source_analysis.get("commit_sha") is not None
                and source_analysis["commit_sha"] != deployment.source_sha
            )
            or (
                source_analysis.get("root_directory") is not None
                and source_analysis["root_directory"] != root
            )
        ):
            raise InvalidInputError("diagnosis source does not match repair")
        if (
            not allowed_paths
            or len(allowed_paths) > 100
            or len(protected_paths) > 100
            or not 1 <= max_changed_files <= 5
            or not 1 <= max_changed_bytes <= 65536
            or deadline.tzinfo is None
            or deadline <= datetime.now(UTC)
            or not 0 < max_cost_usd < float("inf")
        ):
            raise InvalidInputError("invalid repair bounds")
        for path in allowed_paths + protected_paths:
            validate_path(path)
        pinned = await self._source.pin_source(download_url)
        if (
            source_analysis.get("archive_sha256") is not None
            and source_analysis["archive_sha256"] != pinned.archive_sha256
        ):
            raise InvalidInputError("diagnosis archive does not match repair")
        owner, repository = parse_repository_url(service.source_repository_url)
        payload = {
            "schemaVersion": "iris.repair-request.v1",
            "requestId": request_id,
            "scope": {
                "serviceId": service.id,
                "deploymentId": deployment.id,
                "diagnosisId": diagnosis.id,
            },
            "diagnosisResult": raw,
            "planIds": plan_ids,
            "source": {
                "repositoryId": f"{owner}/{repository}",
                "baseCommitSha": deployment.source_sha,
                "rootDirectory": root,
                "downloadUrl": download_url,
                "archiveSha256": pinned.archive_sha256,
                "manifestSha256": pinned.manifest_sha256,
            },
            "policy": {
                "allowedPaths": allowed_paths,
                "protectedPaths": protected_paths,
                "deadline": deadline.isoformat(),
                "maxCostUsd": float(max_cost_usd),
                "maxChangedFiles": max_changed_files,
                "maxChangedBytes": max_changed_bytes,
            },
        }
        return payload

    async def submit_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = str(payload["requestId"])
        result = await self._agent.submit(payload, request_id)
        self.validate_result(
            result, request_id=request_id, base_commit_sha=str(payload["source"]["baseCommitSha"])
        )
        return result

    @staticmethod
    def validate_result(result: dict[str, Any], *, request_id: str, base_commit_sha: str) -> None:
        if result.get("status") in {"RUNNING", "UNKNOWN_OUTCOME", "FAILED"}:
            if result.get("requestId") != request_id:
                raise RepairAgentError(
                    "repair receipt scope mismatch", agent_code="INVALID_RESPONSE"
                )
            return
        if (
            result.get("schemaVersion") != "iris.repair-result.v1"
            or result.get("requestId") != request_id
            or result.get("baseCommitSha") != base_commit_sha
            or result.get("status")
            not in {"candidate_ready", "needs_more_evidence", "configuration_required", "no_change"}
            or result.get("validation") != {"status": "not_run", "owner": "was"}
            or result.get("repositoryPushAuthorized") is not False
            or result.get("deploymentAuthorized") is not False
        ):
            raise RepairAgentError("invalid repair result", agent_code="INVALID_RESPONSE")
        artifacts = result.get("artifacts")
        if not isinstance(artifacts, list):
            raise RepairAgentError("invalid repair artifacts", agent_code="INVALID_RESPONSE")
        names: set[str] = set()
        for artifact in artifacts:
            if (
                not isinstance(artifact, dict)
                or artifact.get("name") not in ARTIFACT_NAMES
                or artifact["name"] in names
                or not isinstance(artifact.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"])
                or type(artifact.get("byteLength")) is not int
                or not 0 <= artifact["byteLength"] <= 2 * 1024 * 1024
            ):
                raise RepairAgentError(
                    "invalid repair artifact metadata", agent_code="INVALID_RESPONSE"
                )
            names.add(artifact["name"])
        if result["status"] == "candidate_ready" and (
            names != ARTIFACT_NAMES
            or any(
                not isinstance(result.get(key), str)
                or not re.fullmatch(r"[0-9a-f]{64}", result[key])
                for key in ("candidateDigest", "candidateManifestSha256")
            )
        ):
            raise RepairAgentError(
                "repair candidate artifacts are incomplete", agent_code="INVALID_RESPONSE"
            )
        if result["status"] != "candidate_ready" and names:
            raise RepairAgentError("unexpected repair artifacts", agent_code="INVALID_RESPONSE")

    async def get_verified_artifact(
        self, request_id: str, name: str, metadata: dict[str, Any]
    ) -> bytes:
        """Download from a constructed internal path and verify sealed transport metadata."""
        if name not in ARTIFACT_NAMES or metadata.get("name") != name:
            raise InvalidInputError("invalid repair artifact name")
        content = await self._agent.get_artifact(request_id, name)
        if sha256(content) != metadata.get("sha256") or len(content) != metadata.get("byteLength"):
            raise RepairAgentError(
                "repair artifact integrity failed", agent_code="INVALID_ARTIFACT"
            )
        return content
