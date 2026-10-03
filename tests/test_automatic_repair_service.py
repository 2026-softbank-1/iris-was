import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest

from app.clients.repair_publication_client import RepairError
from app.core.exceptions import ConflictError, ForbiddenError
from app.models.base import now_utc
from app.services.automatic_repair_service import AutomaticRepairRunner, AutomaticRepairService
from tests.fakes_repair import OWNER

pytest_plugins = ("tests.test_repair_publication_service",)


def automatic(fixture):
    setup, publication, *_ = fixture
    return AutomaticRepairService(setup.session, setup.repairs, setup.repair_service(), publication)


async def test_one_click_authorizes_and_finishes_existing_candidate(publication_setup):
    setup, _, repair, _, calls, refs, _ = publication_setup
    service = automatic(publication_setup)
    await service.resume(OWNER, setup.service.id, repair.id)
    assert repair.request_metadata["autoMerge"] is True
    await service.advance(OWNER, setup.service.id, repair.id)
    assert repair.request_metadata["publication"]["status"] == "MERGED"
    assert refs["main"] == "m" * 40
    assert len(setup.repair_agent.requests) == 1
    count = len(calls)
    await service.advance(OWNER, setup.service.id, repair.id)
    assert len(calls) == count


async def test_loading_or_recovering_a_legacy_candidate_does_not_authorize_merge(publication_setup):
    setup, _, repair, _, calls, _, _ = publication_setup
    await automatic(publication_setup).advance(OWNER, setup.service.id, repair.id)
    assert calls == []
    assert not repair.request_metadata.get("autoMerge")


async def test_permission_and_main_preflight_fail_before_model_or_new_record(publication_setup):
    setup, _, repair, auth, calls, refs, _ = publication_setup
    service = automatic(publication_setup)
    auth.issue_token.side_effect = ForbiddenError("installation needs approval")
    with pytest.raises(ForbiddenError):
        await service.start(
            OWNER,
            setup.service.id,
            repair.deployment_request_id,
            repair.diagnosis_id,
            "automatic-key",
        )
    assert len(setup.repairs.rows) == 1 and len(setup.repair_agent.requests) == 1
    assert calls == []
    auth.issue_token.side_effect = None
    refs["main"] = "e" * 40
    with pytest.raises(RepairError) as error:
        await service.start(
            OWNER,
            setup.service.id,
            repair.deployment_request_id,
            repair.diagnosis_id,
            "automatic-key",
        )
    assert error.value.code == "SOURCE_HEAD_CHANGED"
    assert len(setup.repairs.rows) == 1 and len(setup.repair_agent.requests) == 1


async def test_start_persists_merge_authorization_and_mode_before_generation(publication_setup):
    setup, _, repair, _, _, refs, _ = publication_setup
    service = automatic(publication_setup)
    started = await service.start(
        OWNER, setup.service.id, repair.deployment_request_id, repair.diagnosis_id, "automatic-key"
    )
    assert started.repair.status == "RUNNING"
    assert started.repair.request_metadata["autoMerge"] is True
    assert started.repair.request_metadata["publication"]["status"] == "QUEUED"
    assert len(setup.repair_agent.requests) == 1
    refs["main"] = "e" * 40
    replay = await service.start(
        OWNER, setup.service.id, repair.deployment_request_id, repair.diagnosis_id, "automatic-key"
    )
    assert replay.repair.id == started.repair.id and not replay.is_started
    with pytest.raises(ConflictError):
        await setup.repair_service().start_repair(
            OWNER,
            setup.service.id,
            repair.deployment_request_id,
            repair.diagnosis_id,
            ["R1"],
            "automatic-key",
        )


async def test_wait_for_required_checks_then_merge_same_pr_without_generation(
    publication_setup, monkeypatch
):
    from app.clients.repair_publication_client import GitHubPublisher

    setup, _, repair, _, calls, _, _ = publication_setup
    original = GitHubPublisher.merge_pull_request
    attempts = 0

    async def waiting(self, *args):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RepairError("MERGE_BLOCKED", "checks pending", 409)
        return await original(self, *args)

    monkeypatch.setattr(GitHubPublisher, "merge_pull_request", waiting)
    service = automatic(publication_setup)
    await service.resume(OWNER, setup.service.id, repair.id)
    await service.advance(OWNER, setup.service.id, repair.id)
    assert repair.request_metadata["publication"]["status"] == "WAITING_CHECKS"
    await service.advance(OWNER, setup.service.id, repair.id)
    assert repair.request_metadata["publication"]["status"] == "MERGED"
    assert len(setup.repair_agent.requests) == 1
    assert sum(method == "POST" and path.endswith("/pulls") for method, path, _ in calls) == 1


async def test_server_runner_finishes_after_request_context_is_gone(publication_setup):
    setup, _, repair, _, _, _, _ = publication_setup
    service = automatic(publication_setup)
    await service.resume(OWNER, setup.service.id, repair.id)

    @asynccontextmanager
    async def opener():
        yield service

    async def pending():
        return (
            []
            if repair.request_metadata["publication"]["status"] == "MERGED"
            else [(OWNER, setup.service.id, repair.id)]
        )

    service.pending = pending
    merged = asyncio.Event()
    original_advance = service.advance

    async def advance(*args):
        await original_advance(*args)
        if repair.request_metadata["publication"]["status"] == "MERGED":
            merged.set()

    service.advance = advance
    runner = AutomaticRepairRunner(opener, interval_seconds=0.01)
    runner.start()
    try:
        await asyncio.wait_for(merged.wait(), timeout=3)
    finally:
        await runner.stop()
    assert len(setup.repair_agent.requests) == 1


async def test_expired_authorization_stops_without_github_writes(publication_setup):
    setup, _, repair, _, calls, _, _ = publication_setup
    service = automatic(publication_setup)
    await service.resume(OWNER, setup.service.id, repair.id)
    repair.request_metadata = {
        **repair.request_metadata,
        "autoDeadlineAt": (now_utc() - timedelta(seconds=1)).isoformat(),
    }
    await service.advance(OWNER, setup.service.id, repair.id)
    assert repair.request_metadata["publication"]["errorCode"] == "DEADLINE_EXCEEDED"
    assert calls == []


async def test_unknown_generation_recovers_receipt_then_merges_without_another_post(
    publication_setup,
):
    setup, _, repair, _, _, _, _ = publication_setup
    service = automatic(publication_setup)
    await service.resume(OWNER, setup.service.id, repair.id)
    setup.repair_agent.receipt["result"] = repair.result
    repair.finish("UNKNOWN_OUTCOME")
    await service.advance(OWNER, setup.service.id, repair.id)
    assert repair.request_metadata["publication"]["status"] == "MERGED"
    assert len(setup.repair_agent.requests) == 1


async def test_waiting_runner_never_overwrites_another_replicas_completed_merge(
    publication_setup, monkeypatch
):
    from app.clients.repair_publication_client import GitHubPublisher

    setup, _, repair, _, _, _, _ = publication_setup
    service = automatic(publication_setup)
    await service.resume(OWNER, setup.service.id, repair.id)

    async def concurrent_merge(self, *args):
        raise RepairError("MERGE_BLOCKED", "checks were pending", 409)

    monkeypatch.setattr(GitHubPublisher, "merge_pull_request", concurrent_merge)
    original = setup.repairs.lock_publication
    calls = 0

    async def lock(repair_id):
        nonlocal calls
        calls += 1
        if calls == 3:
            # Another replica finishes after publish/merge release their locks.
            repair.request_metadata = {
                **repair.request_metadata,
                "publication": {"status": "MERGED", "mergeCommitSha": "m" * 40},
            }
        return await original(repair_id)

    setup.repairs.lock_publication = lock
    await service.advance(OWNER, setup.service.id, repair.id)
    assert repair.request_metadata["publication"]["status"] == "MERGED"


async def test_expiry_does_not_overwrite_concurrent_completion(publication_setup):
    setup, _, repair, _, calls, _, _ = publication_setup
    service = automatic(publication_setup)
    await service.resume(OWNER, setup.service.id, repair.id)
    repair.request_metadata = {
        **repair.request_metadata,
        "autoDeadlineAt": (now_utc() - timedelta(seconds=1)).isoformat(),
    }
    original = setup.repairs.lock_publication

    async def lock(repair_id):
        repair.request_metadata = {**repair.request_metadata, "publication": {"status": "MERGED"}}
        return await original(repair_id)

    setup.repairs.lock_publication = lock
    await service.advance(OWNER, setup.service.id, repair.id)
    assert repair.request_metadata["publication"]["status"] == "MERGED"
    assert calls == []


async def test_unsubmitted_queued_job_gets_model_timeout_on_claim_not_enqueue(publication_setup):
    setup, _, original, _, _, _, _ = publication_setup
    service = automatic(publication_setup)
    started = await service.start(
        OWNER,
        setup.service.id,
        original.deployment_request_id,
        original.diagnosis_id,
        "queued-auto",
    )
    repair = started.repair
    repair.deadline_at = now_utc() - timedelta(minutes=1)
    setup.repair_agent.candidate = False
    candidate = await setup.repair_service().get_repair(OWNER, setup.service.id, repair.id)
    assert candidate.status == "RUNNING" and candidate.generation_started_at is None
    assert setup.repair_agent.receipt_calls == []
    await service.advance(OWNER, setup.service.id, repair.id)
    assert repair.status == "SUCCEEDED"
    assert repair.request_metadata["publication"]["status"] == "SKIPPED"
    assert repair.deadline_at > now_utc()
    assert (
        len(setup.repair_agent.requests) == 2
    )  # The existing fixture and one new queued generation.


def configuration_service(fixture, diagnostics=None):
    from cryptography.fernet import Fernet

    from app.core.crypto import VariableCipher
    from app.services.variable_service import VariableService

    setup, publication, *_ = fixture
    cipher = VariableCipher(Fernet.generate_key().decode())
    variables = VariableService(setup.session, setup.services, setup.variables, cipher)
    service = AutomaticRepairService(
        setup.session,
        setup.repairs,
        setup.repair_service(),
        publication,
        diagnostics,
        setup.deployment_request_service(),
        variables,
    )
    return service, variables, cipher


def session_secret_plan(setup, repair, key="SESSION_SECRET"):
    diagnosis = next(d for d in setup.diagnoses.rows if d.id == repair.diagnosis_id)
    diagnosis.result["analysis"]["remediation"]["plans"][0]["changes"] = [
        {"kind": "configuration", "target": key, "instruction": "Restore runtime configuration"}
    ]
    return diagnosis


async def test_missing_session_secret_is_encrypted_then_redeployed_without_code_model(
    publication_setup,
):
    setup, _, original, _, calls, _, _ = publication_setup
    session_secret_plan(setup, original)
    service, _, cipher = configuration_service(publication_setup)
    started = await service.start(
        OWNER,
        setup.service.id,
        original.deployment_request_id,
        original.diagnosis_id,
        "restore-config",
    )
    assert started.repair.request_metadata["strategy"] == "variables"
    await service.advance(OWNER, setup.service.id, started.repair.id)
    stored = await setup.variables.find_by_service_id_and_key(setup.service.id, "SESSION_SECRET")
    assert len(cipher.decrypt(stored.encrypted_value)) == 64
    assert cipher.decrypt(stored.encrypted_value) not in stored.encrypted_value
    publication = started.repair.request_metadata["publication"]
    assert publication["status"] == "REDEPLOY_REQUESTED"
    deployment = await setup.requests.find_by_id_and_service_id(
        publication["redeploymentId"], setup.service.id
    )
    assert "SESSION_SECRET" in deployment.variables_snapshot
    assert deployment.source_sha == original.source_sha
    assert len(setup.repair_agent.requests) == 1
    assert all(method == "GET" for method, _, _ in calls)
    count = len(setup.requests.requests)
    await service.advance(OWNER, setup.service.id, started.repair.id)
    assert len(setup.requests.requests) == count


async def test_stored_session_secret_is_preserved(publication_setup):
    setup, _, original, _, _, _, _ = publication_setup
    session_secret_plan(setup, original)
    service, variables, cipher = configuration_service(publication_setup)
    existing = "existing-private-app-session-secret-" + "x" * 32
    await variables.create_variable(OWNER, setup.service.id, "SESSION_SECRET", existing)
    started = await service.start(
        OWNER,
        setup.service.id,
        original.deployment_request_id,
        original.diagnosis_id,
        "existing-config",
    )
    await service.advance(OWNER, setup.service.id, started.repair.id)
    stored = await setup.variables.find_by_service_id_and_key(setup.service.id, "SESSION_SECRET")
    assert cipher.decrypt(stored.encrypted_value) == existing
    assert existing not in str(started.repair.result) + str(started.repair.request_metadata)


async def test_missing_external_credentials_are_never_invented(publication_setup):
    setup, _, original, _, _, _, _ = publication_setup
    session_secret_plan(setup, original, "EXTERNAL_API_KEY")
    service, _, _ = configuration_service(publication_setup)
    with pytest.raises(RepairError) as error:
        await service.start(
            OWNER,
            setup.service.id,
            original.deployment_request_id,
            original.diagnosis_id,
            "external-config",
        )
    assert error.value.code == "CONFIGURATION_VALUES_REQUIRED"
    assert setup.variables.variables == []
    assert len(setup.repairs.rows) == 1


async def test_failed_deployment_without_diagnosis_queues_diagnosis_then_configuration_fix(
    publication_setup,
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.enums import DiagnosisStatus

    setup, _, original, _, _, _, _ = publication_setup
    finished = session_secret_plan(setup, original).result
    diagnosis = setup.diagnoses.seed(original.deployment_request_id, DiagnosisStatus.RUNNING)
    diagnostics = AsyncMock()
    diagnostics.start_diagnosis.return_value = SimpleNamespace(diagnosis=diagnosis, is_started=True)

    async def finish(*args):
        diagnosis.status = DiagnosisStatus.SUCCEEDED
        diagnosis.result = finished
        return diagnosis

    diagnostics.run_diagnosis.side_effect = finish
    service, _, _ = configuration_service(publication_setup, diagnostics)
    started = await service.start(
        OWNER, setup.service.id, original.deployment_request_id, None, "diagnose-and-fix"
    )
    assert started.repair.request_metadata["ownsDiagnosis"] is True
    assert started.repair.request_metadata["awaitingDiagnosis"] is True
    assert started.repair.request_metadata["publication"]["status"] == "DIAGNOSING"
    await service.advance(OWNER, setup.service.id, started.repair.id)
    assert started.repair.request_metadata["publication"]["status"] == "REDEPLOY_REQUESTED"
    assert diagnostics.run_diagnosis.await_count == 1
    assert len(setup.repair_agent.requests) == 1


async def test_code_merge_requests_redeployment_even_when_webhook_auto_deploy_is_off(
    publication_setup,
):
    setup, publication, repair, _, _, _, _ = publication_setup
    setup.service.is_auto_deploy = False
    service = AutomaticRepairService(
        setup.session,
        setup.repairs,
        setup.repair_service(),
        publication,
        deployer=setup.deployment_request_service(),
    )
    await service.resume(OWNER, setup.service.id, repair.id)
    await service.advance(OWNER, setup.service.id, repair.id)
    state = repair.request_metadata["publication"]
    assert state["status"] == "MERGED" and state["redeploymentId"]
    deployment = await setup.requests.find_by_id_and_service_id(
        state["redeploymentId"], setup.service.id
    )
    assert deployment.source_sha == state["mergeCommitSha"]


@pytest.mark.parametrize("previous_status", ["FAILED", "SUCCEEDED"])
async def test_configuration_repair_creates_fresh_snapshot_instead_of_reusing_same_sha(
    publication_setup, previous_status
):
    from app.enums import DeploymentStatus

    setup, _, original, _, _, _, _ = publication_setup
    session_secret_plan(setup, original)
    service, variables, cipher = configuration_service(publication_setup)
    old = "old-session-secret-" + "a" * 48
    current = "current-session-secret-" + "b" * 48
    await variables.create_variable(OWNER, setup.service.id, "SESSION_SECRET", current)
    previous = await setup.requests.find_by_id_and_service_id(
        original.deployment_request_id, setup.service.id
    )
    previous.variables_snapshot = {"SESSION_SECRET": cipher.encrypt(old)}
    previous.status = DeploymentStatus(previous_status)
    # Keep the diagnostic source failed while modelling another same-SHA historical deployment.
    if previous_status == "SUCCEEDED":
        from copy import copy

        historical = copy(previous)
        historical.id = previous.id + 100
        historical.idempotency_key = "historical-success"
        setup.requests.requests.append(historical)
        previous.status = DeploymentStatus.FAILED
    count = len(setup.requests.requests)
    started = await service.start(
        OWNER, setup.service.id, previous.id, original.diagnosis_id, "fresh-config"
    )
    await service.advance(OWNER, setup.service.id, started.repair.id)
    state = started.repair.request_metadata["publication"]
    assert state["status"] == "REDEPLOY_REQUESTED"
    assert len(setup.requests.requests) == count + 1
    fresh = await setup.requests.find_by_id_and_service_id(
        state["redeploymentId"], setup.service.id
    )
    assert fresh.id != previous.id
    assert fresh.status == DeploymentStatus.QUEUED
    assert cipher.decrypt(fresh.variables_snapshot["SESSION_SECRET"]) == current
    await service.advance(OWNER, setup.service.id, started.repair.id)
    assert len(setup.requests.requests) == count + 1
