from datetime import timedelta

import pytest

from app.clients.repair_agent_client import RepairAgentError
from app.core.exceptions import ConflictError, InvalidInputError, ServiceNotFoundError
from app.enums import DeploymentStatus
from app.models.base import now_utc
from tests.fakes_repair import OWNER, RepairSetup


@pytest.fixture
async def setup() -> RepairSetup:
    setup = RepairSetup()
    await setup.build()
    return setup


async def test_repair_commit_before_source_and_one_generation(setup: RepairSetup) -> None:
    deployment, diagnosis = setup.add_repair_inputs()
    service = setup.repair_service()
    started = await service.start_repair(
        OWNER, setup.service.id, deployment, diagnosis, ["R1"], "key"
    )
    assert started.is_started and started.repair.status == "RUNNING"
    assert setup.repair_agent.requests == [] and setup.snapshots.build_ids == []
    await service.run_repair(OWNER, setup.service.id, started.repair.id)
    await service.run_repair(OWNER, setup.service.id, started.repair.id)
    assert len(setup.repair_agent.requests) == 1
    assert started.repair.status == "SUCCEEDED"
    assert setup.requests.requests[0].status == DeploymentStatus.FAILED
    assert "downloadUrl" not in str(started.repair.request_metadata)


async def test_repair_idempotency_same_and_conflicting_input(setup: RepairSetup) -> None:
    deployment, diagnosis = setup.add_repair_inputs()
    service = setup.repair_service()
    first = await service.start_repair(
        OWNER, setup.service.id, deployment, diagnosis, ["R1"], "key"
    )
    same = await service.start_repair(OWNER, setup.service.id, deployment, diagnosis, ["R1"], "key")
    assert not same.is_started and same.repair.id == first.repair.id
    other_deployment, other_diagnosis = setup.add_repair_inputs()
    with pytest.raises(ConflictError):
        await service.start_repair(
            OWNER, setup.service.id, other_deployment, other_diagnosis, ["R1"], "key"
        )
    with pytest.raises(ConflictError):
        await service.start_repair(
            OWNER, setup.service.id, deployment, diagnosis, ["R1"], "new-key"
        )


async def test_repair_owner_and_diagnosis_pairing_enforced(setup: RepairSetup) -> None:
    deployment, diagnosis = setup.add_repair_inputs()
    other_deployment, other_diagnosis = setup.add_repair_inputs()
    service = setup.repair_service()
    with pytest.raises(ServiceNotFoundError):
        await service.start_repair(
            OWNER + 1, setup.service.id, deployment, diagnosis, ["R1"], "key"
        )
    with pytest.raises(InvalidInputError):
        await service.start_repair(
            OWNER, setup.service.id, deployment, other_diagnosis, ["R1"], "key"
        )
    with pytest.raises(InvalidInputError):
        await service.start_repair(
            OWNER, setup.service.id, other_deployment, other_diagnosis, ["absent"], "key"
        )
    assert setup.repairs.rows == []


async def test_repair_unknown_never_resubmits_and_recovers_receipt(setup: RepairSetup) -> None:
    deployment, diagnosis = setup.add_repair_inputs()
    service = setup.repair_service()
    started = await service.start_repair(
        OWNER, setup.service.id, deployment, diagnosis, ["R1"], "key"
    )
    setup.repair_agent.response = RepairAgentError("uncertain", agent_code="UNKNOWN_OUTCOME")
    await service.run_repair(OWNER, setup.service.id, started.repair.id)
    assert started.repair.status == "UNKNOWN_OUTCOME"
    await service.get_repair(OWNER, setup.service.id, started.repair.id)
    await service.start_repair(OWNER, setup.service.id, deployment, diagnosis, ["R1"], "key")
    assert len(setup.repair_agent.requests) == 1
    assert setup.repair_agent.receipt_calls == [started.repair.agent_request_id]
    setup.repair_agent.response = None
    # Construct a completed provider receipt without another service submission.
    payload = setup.repair_agent.requests[0]
    await setup.repair_agent.submit(payload, started.repair.agent_request_id)
    before = len(setup.repair_agent.requests)
    recovered = await service.get_repair(OWNER, setup.service.id, started.repair.id)
    assert recovered.status == "SUCCEEDED" and len(setup.repair_agent.requests) == before


async def test_repair_stale_running_only_reads_receipt(setup: RepairSetup) -> None:
    deployment, diagnosis = setup.add_repair_inputs()
    service = setup.repair_service()
    started = await service.start_repair(
        OWNER, setup.service.id, deployment, diagnosis, ["R1"], "key"
    )
    started.repair.deadline_at = now_utc() - timedelta(minutes=1)
    await service.get_repair(OWNER, setup.service.id, started.repair.id)
    assert started.repair.status == "UNKNOWN_OUTCOME"
    assert setup.repair_agent.requests == []


async def test_repair_artifact_owned_and_verified(setup: RepairSetup) -> None:
    deployment, diagnosis = setup.add_repair_inputs()
    setup.repair_agent.candidate = True
    service = setup.repair_service()
    started = await service.start_repair(
        OWNER, setup.service.id, deployment, diagnosis, ["R1"], "key"
    )
    await service.run_repair(OWNER, setup.service.id, started.repair.id)
    content = await service.get_artifact(OWNER, setup.service.id, started.repair.id, "patch.diff")
    assert content == b"patch.diff"
    with pytest.raises(ServiceNotFoundError):
        await service.get_artifact(OWNER + 1, setup.service.id, started.repair.id, "patch.diff")
    setup.repair_agent.artifacts["patch.diff"] = b"corrupt"
    with pytest.raises(RepairAgentError):
        await service.get_artifact(OWNER, setup.service.id, started.repair.id, "patch.diff")


async def test_repair_rejects_source_after_retention(setup: RepairSetup) -> None:
    deployment, diagnosis = setup.add_repair_inputs()
    setup.builds.builds[0].created_at = now_utc() - timedelta(hours=24)
    with pytest.raises(ConflictError):
        await setup.repair_service().start_repair(
            OWNER, setup.service.id, deployment, diagnosis, ["R1"], "key"
        )


async def test_repair_freezes_service_root_repository_and_policy(setup: RepairSetup) -> None:
    deployment, diagnosis = setup.add_repair_inputs()
    service = setup.repair_service()
    started = await service.start_repair(
        OWNER, setup.service.id, deployment, diagnosis, ["R1"], "key"
    )
    frozen_repository = started.repair.source_repository_url
    setup.service.source_repository_url = "https://github.com/outsider/other"
    setup.service.root_directory = "unrelated"
    await service.run_repair(OWNER, setup.service.id, started.repair.id)
    payload = setup.repair_agent.requests[0]
    assert frozen_repository.endswith(payload["source"]["repositoryId"])
    assert payload["source"]["rootDirectory"] == "."
    assert payload["policy"]["allowedPaths"] == ["*"]


async def test_repair_rejects_receipt_for_wrong_input_digest(setup: RepairSetup) -> None:
    deployment, diagnosis = setup.add_repair_inputs()
    service = setup.repair_service()
    started = await service.start_repair(
        OWNER, setup.service.id, deployment, diagnosis, ["R1"], "key"
    )
    await service.run_repair(OWNER, setup.service.id, started.repair.id)
    started.repair.finish("UNKNOWN_OUTCOME")
    assert isinstance(setup.repair_agent.receipt, dict)
    setup.repair_agent.receipt["result"]["inputDigest"] = "0" * 64
    polled = await service.get_repair(OWNER, setup.service.id, started.repair.id)
    assert polled.status == "UNKNOWN_OUTCOME"
    assert polled.result is None


async def test_repair_idempotency_detects_changed_raw_diagnosis(setup: RepairSetup) -> None:
    deployment, diagnosis = setup.add_repair_inputs()
    service = setup.repair_service()
    await service.start_repair(OWNER, setup.service.id, deployment, diagnosis, ["R1"], "key")
    setup.diagnoses.rows[0].result["analysis"]["summary"] = "altered"
    with pytest.raises(ConflictError):
        await service.start_repair(OWNER, setup.service.id, deployment, diagnosis, ["R1"], "key")


@pytest.mark.parametrize("status", [401, 422, 429])
async def test_repair_definitive_agent_rejection_is_failed_without_receipt_poll(
    setup: RepairSetup,
    status: int,
) -> None:
    deployment, diagnosis = setup.add_repair_inputs()
    service = setup.repair_service()
    started = await service.start_repair(
        OWNER, setup.service.id, deployment, diagnosis, ["R1"], "rejected-key"
    )
    setup.repair_agent.response = RepairAgentError(
        "rejected", agent_code="REJECTED", agent_status=status
    )
    await service.run_repair(OWNER, setup.service.id, started.repair.id)
    assert started.repair.status == "FAILED"
    assert started.repair.error_code == "REJECTED"
    await service.get_repair(OWNER, setup.service.id, started.repair.id)
    assert setup.repair_agent.receipt_calls == []
    assert len(setup.repair_agent.requests) == 1
