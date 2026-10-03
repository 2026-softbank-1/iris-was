from datetime import timedelta

import pytest

from app.core.exceptions import (
    DeploymentNotFailedError,
    DiagnosisNotFoundError,
    DiagnosisNotSucceededError,
    ServiceNotFoundError,
    SourceSnapshotUnavailableError,
)
from app.enums import DeploymentStatus, DiagnosisStatus
from app.models.base import now_utc
from app.services.repair_context_service import RepairContextService
from tests.fakes_diagnosis import OWNER, SOURCE_SHA, DiagnosisSetup, valid_agent_result

ARCHIVE_SHA = "a" * 64
MANIFEST_SHA = "b" * 64


def _service(setup: DiagnosisSetup, *, snapshots: bool = True) -> RepairContextService:
    return RepairContextService(
        setup.services,  # type: ignore[arg-type]
        setup.requests,  # type: ignore[arg-type]
        setup.builds,  # type: ignore[arg-type]
        setup.diagnoses,  # type: ignore[arg-type]
        setup.snapshots if snapshots else None,
    )


async def _failed_with_diagnosis(setup: DiagnosisSetup, *, pinned: bool = True):  # type: ignore[no-untyped-def]
    request = setup.add_request()
    build = setup.add_build(request)
    if pinned:
        build.record_source_digests(ARCHIVE_SHA, MANIFEST_SHA)
    diagnosis = setup.diagnoses.seed(
        request.id, DiagnosisStatus.SUCCEEDED, result=valid_agent_result()
    )
    return request, build, diagnosis


@pytest.fixture
async def setup() -> DiagnosisSetup:
    return await DiagnosisSetup().build()


async def test_get_repair_context_returns_pinned_source_and_original_diagnosis(
    setup: DiagnosisSetup,
) -> None:
    request, build, diagnosis = await _failed_with_diagnosis(setup)
    setup.service.root_directory = "apps/api/"

    context = await _service(setup).get_repair_context(
        OWNER, setup.service.id, request.id, diagnosis.id
    )

    assert context.deployment_id == request.id and context.diagnosis_id == diagnosis.id
    assert context.branch == setup.service.source_branch
    assert context.repository_url == setup.service.source_repository_url
    assert context.source.commit_sha == SOURCE_SHA
    assert context.source.root_directory == "apps/api"
    assert context.source.archive_sha256 == ARCHIVE_SHA
    assert context.source.manifest_sha256 == MANIFEST_SHA
    assert context.source.download_url.endswith(f"snapshots/{build.id}.tar.gz?sig=x")
    assert context.source.expires_at > now_utc()
    # 진단 원문은 키와 null 값까지 그대로여야 한다.
    assert context.diagnosis_result == valid_agent_result()
    assert context.diagnosis_result["error"] is None
    assert setup.snapshots.build_ids == [build.id]


async def test_get_repair_context_without_frozen_hashes_leaves_them_empty(
    setup: DiagnosisSetup,
) -> None:
    request, _, diagnosis = await _failed_with_diagnosis(setup, pinned=False)

    context = await _service(setup).get_repair_context(
        OWNER, setup.service.id, request.id, diagnosis.id
    )

    assert context.source.archive_sha256 is None
    assert context.source.manifest_sha256 is None
    assert context.source.root_directory == "."


async def test_get_repair_context_rolled_back_request_uses_original_builds_snapshot(
    setup: DiagnosisSetup,
) -> None:
    original = setup.add_request(DeploymentStatus.SUCCEEDED, failure_code=None)
    original_build = setup.add_build(original)
    original_build.record_source_digests(ARCHIVE_SHA, MANIFEST_SHA)
    rollback = setup.add_request(source_deployment_request_id=original.id)
    diagnosis = setup.diagnoses.seed(
        rollback.id, DiagnosisStatus.SUCCEEDED, result=valid_agent_result()
    )

    context = await _service(setup).get_repair_context(
        OWNER, setup.service.id, rollback.id, diagnosis.id
    )

    assert setup.snapshots.build_ids == [original_build.id]
    assert context.source.manifest_sha256 == MANIFEST_SHA


async def test_get_repair_context_other_owner_hides_service(setup: DiagnosisSetup) -> None:
    request, _, diagnosis = await _failed_with_diagnosis(setup)

    with pytest.raises(ServiceNotFoundError):
        await _service(setup).get_repair_context(
            OWNER + 1, setup.service.id, request.id, diagnosis.id
        )
    assert setup.snapshots.build_ids == []


async def test_get_repair_context_requires_failed_deployment(setup: DiagnosisSetup) -> None:
    request = setup.add_request(DeploymentStatus.SUCCEEDED, failure_code=None)
    setup.add_build(request)
    diagnosis = setup.diagnoses.seed(
        request.id, DiagnosisStatus.SUCCEEDED, result=valid_agent_result()
    )

    with pytest.raises(DeploymentNotFailedError):
        await _service(setup).get_repair_context(OWNER, setup.service.id, request.id, diagnosis.id)


async def test_get_repair_context_rejects_diagnosis_of_another_deployment(
    setup: DiagnosisSetup,
) -> None:
    request, _, _ = await _failed_with_diagnosis(setup)
    other = setup.add_request()
    foreign = setup.diagnoses.seed(other.id, DiagnosisStatus.SUCCEEDED, result=valid_agent_result())

    with pytest.raises(DiagnosisNotFoundError):
        await _service(setup).get_repair_context(OWNER, setup.service.id, request.id, foreign.id)
    assert setup.snapshots.build_ids == []


async def test_get_repair_context_unknown_diagnosis_is_not_found(setup: DiagnosisSetup) -> None:
    request, _, _ = await _failed_with_diagnosis(setup)

    with pytest.raises(DiagnosisNotFoundError):
        await _service(setup).get_repair_context(OWNER, setup.service.id, request.id, 999)


@pytest.mark.parametrize("status", [DiagnosisStatus.RUNNING, DiagnosisStatus.FAILED])
async def test_get_repair_context_requires_succeeded_diagnosis(
    setup: DiagnosisSetup, status: DiagnosisStatus
) -> None:
    request = setup.add_request()
    setup.add_build(request)
    diagnosis = setup.diagnoses.seed(request.id, status)

    with pytest.raises(DiagnosisNotSucceededError):
        await _service(setup).get_repair_context(OWNER, setup.service.id, request.id, diagnosis.id)
    assert setup.snapshots.build_ids == []


async def test_get_repair_context_without_snapshot_store_is_unavailable(
    setup: DiagnosisSetup,
) -> None:
    request, _, diagnosis = await _failed_with_diagnosis(setup)

    with pytest.raises(SourceSnapshotUnavailableError):
        await _service(setup, snapshots=False).get_repair_context(
            OWNER, setup.service.id, request.id, diagnosis.id
        )


async def test_get_repair_context_expired_snapshot_is_unavailable(setup: DiagnosisSetup) -> None:
    request, build, diagnosis = await _failed_with_diagnosis(setup)
    build.created_at = now_utc() - timedelta(hours=24)

    with pytest.raises(SourceSnapshotUnavailableError):
        await _service(setup).get_repair_context(OWNER, setup.service.id, request.id, diagnosis.id)
    assert setup.snapshots.build_ids == []


async def test_get_repair_context_build_never_started_is_unavailable(
    setup: DiagnosisSetup,
) -> None:
    request = setup.add_request()
    setup.add_build(request, codebuild_build_id=None)
    diagnosis = setup.diagnoses.seed(
        request.id, DiagnosisStatus.SUCCEEDED, result=valid_agent_result()
    )

    with pytest.raises(SourceSnapshotUnavailableError):
        await _service(setup).get_repair_context(OWNER, setup.service.id, request.id, diagnosis.id)


async def test_get_repair_context_unknown_commit_is_unavailable(setup: DiagnosisSetup) -> None:
    request = setup.add_request(source_sha="main")
    setup.add_build(request)
    diagnosis = setup.diagnoses.seed(
        request.id, DiagnosisStatus.SUCCEEDED, result=valid_agent_result()
    )

    with pytest.raises(SourceSnapshotUnavailableError):
        await _service(setup).get_repair_context(OWNER, setup.service.id, request.id, diagnosis.id)
