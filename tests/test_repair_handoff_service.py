import io
import tarfile
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.clients.repair_agent_client import RepairAgentError
from app.core.exceptions import InvalidInputError
from app.enums import DeploymentStatus, DiagnosisStatus
from app.models.deployment_diagnosis import DeploymentDiagnosis
from app.models.deployment_request import DeploymentRequest
from app.models.service import Service
from app.services.repair_handoff_service import RepairHandoffService
from app.services.repair_source import canonical_json, pin_archive, sha256


def archive(entries: list[tuple[str, bytes, bytes]]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as tar:
        for name, data, kind in entries:
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.type = kind
            member.mode = 0o755 if name.endswith("run.sh") else 0o644
            tar.addfile(member, io.BytesIO(data))
    return output.getvalue()


def test_repair_manifest_matches_exact_sorted_canonical_contract() -> None:
    data = archive(
        [
            ("wrapper/z.py", b"print('hello')", tarfile.REGTYPE),
            ("wrapper/run.sh", b"echo ok", tarfile.REGTYPE),
        ]
    )
    source = pin_archive(data)
    expected = [
        {"path": "run.sh", "sha256": sha256(b"echo ok"), "mode": "100755", "size": 7},
        {"path": "z.py", "sha256": sha256(b"print('hello')"), "mode": "100644", "size": 14},
    ]
    assert source.archive_sha256 == sha256(data)
    assert source.manifest_sha256 == sha256(canonical_json(expected))


@pytest.mark.parametrize(
    "entries",
    [
        [("../bad", b"x", tarfile.REGTYPE)],
        [("wrapper/a", b"x", tarfile.SYMTYPE)],
        [("wrapper/a", b"x", tarfile.REGTYPE), ("wrapper/a", b"y", tarfile.REGTYPE)],
        [("wrapper/a", b"x", tarfile.REGTYPE), ("wrapper/a/b", b"y", tarfile.REGTYPE)],
    ],
)
def test_repair_source_rejects_unsafe_archives(entries: list[tuple[str, bytes, bytes]]) -> None:
    with pytest.raises(InvalidInputError):
        pin_archive(archive(entries))


class SourceFake:
    async def pin_source(self, download_url: str) -> Any:
        return pin_archive(archive([("wrapper/app.py", b"print('ok')", tarfile.REGTYPE)]))


class AgentFake:
    async def submit(self, payload: dict[str, Any], request_id: str) -> dict[str, Any]:
        raise AssertionError("preparation must not call model")

    async def get_receipt(self, request_id: str) -> dict[str, Any]:
        return {}

    async def get_artifact(self, request_id: str, name: str) -> bytes:
        return b"sealed"


async def test_repair_preparation_preserves_raw_diagnosis_and_frozen_commit() -> None:
    raw = {
        "schema_version": "diagnosis-result.v3",
        "job_status": "succeeded",
        "analysis": {"remediation": {"plans": [{"id": "R1", "changes": [{"kind": "code"}]}]}},
        "backend_context": {"service_id": 1, "deployment_id": 2},
        "extra_retained": "yes",
    }
    service = Service(
        id=1, source_repository_url="https://github.com/org/repo", root_directory=None
    )
    request = DeploymentRequest(
        id=2, service_id=1, source_sha="a" * 40, status=DeploymentStatus.FAILED
    )
    diagnosis = DeploymentDiagnosis(
        id=3, deployment_request_id=2, status=DiagnosisStatus.SUCCEEDED, result=raw
    )
    handoff = RepairHandoffService(AgentFake(), SourceFake())
    payload = await handoff.prepare_request(
        service,
        request,
        diagnosis,
        request_id="was-repair-4",
        plan_ids=["R1"],
        download_url="https://source.test/a?secret=yes",
        allowed_paths=["*.py"],
        protected_paths=[],
        deadline=datetime.now(UTC) + timedelta(minutes=5),
        max_cost_usd=1,
    )
    assert payload["diagnosisResult"] is raw
    assert payload["source"]["baseCommitSha"] == "a" * 40
    assert payload["source"]["repositoryId"] == "org/repo"
    assert payload["scope"] == {"serviceId": 1, "deploymentId": 2, "diagnosisId": 3}


async def test_repair_artifact_digest_checked_and_metadata_url_ignored() -> None:
    handoff = RepairHandoffService(AgentFake(), SourceFake())
    metadata = {
        "name": "patch.diff",
        "sha256": sha256(b"sealed"),
        "byteLength": 6,
        "url": "https://evil.example/secret",
    }
    assert await handoff.get_verified_artifact("was-repair-4", "patch.diff", metadata) == b"sealed"
    metadata["sha256"] = "0" * 64
    with pytest.raises(RepairAgentError):
        await handoff.get_verified_artifact("was-repair-4", "patch.diff", metadata)


def test_repair_candidate_never_counts_as_validated() -> None:
    with pytest.raises(RepairAgentError):
        RepairHandoffService.validate_result(
            {
                "schemaVersion": "iris.repair-result.v1",
                "requestId": "x",
                "baseCommitSha": "a" * 40,
                "status": "candidate_ready",
                "validation": {"status": "passed", "owner": "was"},
            },
            request_id="x",
            base_commit_sha="a" * 40,
        )
