from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from itertools import count
from typing import Any

from app.clients.repair_agent_client import ARTIFACT_NAMES, RepairAgentError
from app.core.exceptions import NotFoundError
from app.enums import DiagnosisStatus
from app.models.base import now_utc
from app.models.deployment_repair import DeploymentRepair
from app.services.repair_handoff_service import RepairHandoffService
from app.services.repair_service import RepairService, RepairServiceOpener, _digest
from app.services.repair_source import PinnedRepairSource, sha256
from tests.fakes_diagnosis import OWNER, DiagnosisSetup, valid_agent_result


class FakeRepairRepository:
    def __init__(self) -> None:
        self.rows: list[DeploymentRepair] = []
        self._ids = count(1)

    async def get_by_id(self, repair_id: int) -> DeploymentRepair:
        row = next((row for row in self.rows if row.id == repair_id), None)
        if row is None:
            raise NotFoundError("repair not found")
        return row

    async def find_by_service_id_and_key(
        self, service_id: int, key: str
    ) -> DeploymentRepair | None:
        return next(
            (
                row
                for row in self.rows
                if row.service_id == service_id and row.idempotency_key == key
            ),
            None,
        )

    async def find_running_by_deployment_request_id(
        self, deployment_request_id: int
    ) -> DeploymentRepair | None:
        return next(
            (
                row
                for row in self.rows
                if row.deployment_request_id == deployment_request_id and row.status == "RUNNING"
            ),
            None,
        )

    async def latest(
        self, service_id: int, deployment_id: int, diagnosis_id: int
    ) -> DeploymentRepair:
        matches = [
            r
            for r in self.rows
            if r.service_id == service_id
            and r.deployment_request_id == deployment_id
            and r.diagnosis_id == diagnosis_id
        ]
        if not matches:
            raise NotFoundError("repair not found")
        return matches[-1]

    async def lock_publication(self, repair_id: int) -> DeploymentRepair:
        return await self.get_by_id(repair_id)

    async def add_running_if_absent(self, values: dict[str, Any]) -> DeploymentRepair | None:
        if (
            await self.find_running_by_deployment_request_id(values["deployment_request_id"])
            is not None
            or await self.find_by_service_id_and_key(
                values["service_id"], values["idempotency_key"]
            )
            is not None
        ):
            return None
        row = DeploymentRepair(
            id=next(self._ids),
            status="RUNNING",
            created_at=now_utc(),
            updated_at=now_utc(),
            **values,
        )
        self.rows.append(row)
        return row

    async def claim_generation(
        self, repair_id: int, *, deadline_at: datetime | None = None
    ) -> bool:
        row = await self.get_by_id(repair_id)
        if row.status != "RUNNING" or row.generation_started_at is not None:
            return False
        row.generation_started_at = now_utc()
        if deadline_at is not None:
            row.deadline_at = deadline_at
        return True


class FakeRepairSourceClient:
    async def pin_source(self, download_url: str) -> PinnedRepairSource:
        return PinnedRepairSource("a" * 64, "b" * 64, {})


class FakeRepairAgentClient:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.receipt_calls: list[str] = []
        self.response: dict[str, Any] | Exception | None = None
        self.receipt: dict[str, Any] | Exception = RepairAgentError(
            "not found", agent_code="REPAIR_NOT_FOUND"
        )
        self.artifacts = {name: name.encode() for name in ARTIFACT_NAMES}
        self.candidate = False

    async def submit(self, payload: dict[str, Any], request_id: str) -> dict[str, Any]:
        import copy

        self.requests.append(payload)
        if isinstance(self.response, Exception):
            raise self.response
        if self.response is not None:
            return self.response
        stable = copy.deepcopy(payload)
        stable["source"].pop("downloadUrl")
        stable["policy"]["deadline"] = stable["policy"]["deadline"].replace("+00:00", "Z")
        result = {
            "schemaVersion": "iris.repair-result.v1",
            "requestId": request_id,
            "baseCommitSha": payload["source"]["baseCommitSha"],
            "inputDigest": _digest(stable),
            "status": "candidate_ready" if self.candidate else "no_change",
            "validation": {"status": "not_run", "owner": "was"},
            "repositoryPushAuthorized": False,
            "deploymentAuthorized": False,
            "candidateDigest": "c" * 64,
            "candidateManifestSha256": "d" * 64,
            "artifacts": [
                {
                    "name": name,
                    "sha256": sha256(content),
                    "byteLength": len(content),
                    "url": "internal",
                }
                for name, content in self.artifacts.items()
            ]
            if self.candidate
            else [],
        }
        self.receipt = {
            "requestId": request_id,
            "inputDigest": result["inputDigest"],
            "status": "SUCCEEDED",
            "result": result,
        }
        return result

    async def get_receipt(self, request_id: str) -> dict[str, Any]:
        self.receipt_calls.append(request_id)
        if isinstance(self.receipt, Exception):
            raise self.receipt
        return self.receipt

    async def get_artifact(self, request_id: str, name: str) -> bytes:
        return self.artifacts[name]


class RepairSetup(DiagnosisSetup):
    def __init__(self) -> None:
        super().__init__()
        self.repairs = FakeRepairRepository()
        self.repair_agent = FakeRepairAgentClient()

    def repair_service(self, *, configured: bool = True) -> RepairService:
        return RepairService(
            self.session,
            self.services,
            self.requests,
            self.builds,
            self.diagnoses,
            self.repairs,
            self.repair_agent if configured else None,
            RepairHandoffService(self.repair_agent, FakeRepairSourceClient())
            if configured
            else None,
            self.snapshots if configured else None,
        )  # type: ignore[arg-type]

    def repair_service_opener(self, *, configured: bool = True) -> RepairServiceOpener:
        @asynccontextmanager
        async def open_service() -> AsyncIterator[RepairService]:
            yield self.repair_service(configured=configured)

        return open_service

    def add_repair_inputs(self) -> tuple[int, int]:
        request = self.add_request()
        self.add_build(request)
        raw = valid_agent_result()
        raw["analysis"]["remediation"]["plans"][0]["changes"][0]["kind"] = "code"
        diagnosis = self.diagnoses.seed(request.id, DiagnosisStatus.SUCCEEDED, result=raw)
        return request.id, diagnosis.id


__all__ = ["OWNER", "RepairSetup"]
