from datetime import timedelta
from typing import Any

import pytest

from app.core.exceptions import (
    DeploymentNotFailedError,
    DeploymentRequestNotFoundError,
    DiagnosisAgentError,
    DiagnosisInProgressError,
    DiagnosisLogsUnavailableError,
    DiagnosisNotFoundError,
    NotConfiguredError,
    ServiceNotFoundError,
)
from app.enums import DeploymentStatus, DiagnosisStatus, FailureCode
from app.services.diagnosis_service import (
    INTERNAL_ERROR_CODE,
    LOG_BUDGETS_BYTES,
    STALE_ERROR_CODE,
    run_diagnosis_in_background,
)
from tests.fakes_diagnosis import (
    OWNER,
    SOURCE_SHA,
    DiagnosisSetup,
    make_log,
    make_log_tail,
    valid_agent_result,
)


@pytest.fixture
async def setup() -> DiagnosisSetup:
    return await DiagnosisSetup().build()


async def test_diagnose_failed_deployment_saves_result_and_sends_logs_in_time_order(
    setup: DiagnosisSetup,
) -> None:
    request = setup.add_request()
    setup.add_build(request, attempt=2)

    diagnosis = await setup.diagnosis_service().diagnose(OWNER, setup.service.id, request.id)

    assert diagnosis.status == DiagnosisStatus.SUCCEEDED
    assert diagnosis.result is not None
    assert diagnosis.result["analysis"]["summary"].startswith("DATABASE_URL")
    assert diagnosis.finished_at is not None
    assert diagnosis.requested_by == OWNER
    data = setup.agent.requests[0]
    assert (data["projectId"], data["serviceId"], data["deploymentId"]) == (
        setup.service.project_id,
        setup.service.id,
        request.id,
    )
    assert data["attemptId"] == 2
    assert data["deploymentStatus"] == "FAILED"
    assert data["failedStage"] == "runtime"
    assert "exitCode" not in data
    assert [log["text"] for log in data["logs"]] == ["line 1", "line 2", "line 3"]
    assert [log["sequence"] for log in data["logs"]] == [1, 2, 3]
    assert {log["stage"] for log in data["logs"]} == {"runtime"}
    assert data["logRange"]["isComplete"] is True
    assert "source" not in data
    assert setup.loki.calls[0]["namespace"] == f"svc-{setup.service.id}"


@pytest.mark.parametrize(
    ("failure_code", "stage"),
    [
        (FailureCode.DEPLOY_FAILED, "runtime"),
        (FailureCode.DEPLOY_TIMED_OUT, "deploy"),
        (FailureCode.DEPLOY_INFRA_ERROR, "deploy"),
    ],
)
async def test_diagnose_maps_failure_code_to_failed_stage(
    setup: DiagnosisSetup, failure_code: FailureCode, stage: str
) -> None:
    request = setup.add_request(failure_code=failure_code)

    await setup.diagnosis_service().diagnose(OWNER, setup.service.id, request.id)

    assert setup.agent.requests[0]["failedStage"] == stage


async def test_diagnose_without_failure_code_omits_failed_stage(setup: DiagnosisSetup) -> None:
    request = setup.add_request(DeploymentStatus.MANUAL_INTERVENTION, failure_code=None)

    await setup.diagnosis_service().diagnose(OWNER, setup.service.id, request.id)

    assert "failedStage" not in setup.agent.requests[0]


@pytest.mark.parametrize(
    "status", [DeploymentStatus.ROLLED_BACK, DeploymentStatus.MANUAL_INTERVENTION]
)
async def test_diagnose_rolled_back_or_manual_intervention_request_is_allowed(
    setup: DiagnosisSetup, status: DeploymentStatus
) -> None:
    request = setup.add_request(status)

    diagnosis = await setup.diagnosis_service().diagnose(OWNER, setup.service.id, request.id)

    assert diagnosis.status == DiagnosisStatus.SUCCEEDED


@pytest.mark.parametrize(
    "status",
    [
        DeploymentStatus.QUEUED,
        DeploymentStatus.BUILDING,
        DeploymentStatus.DEPLOYING,
        DeploymentStatus.SUCCEEDED,
        DeploymentStatus.SUPERSEDED,
    ],
)
async def test_diagnose_not_failed_request_raises_conflict_without_calling_agent(
    setup: DiagnosisSetup, status: DeploymentStatus
) -> None:
    request = setup.add_request(status, failure_code=None)

    with pytest.raises(DeploymentNotFailedError):
        await setup.diagnosis_service().diagnose(OWNER, setup.service.id, request.id)

    assert setup.agent.requests == []
    assert setup.diagnoses.rows == []


async def test_diagnose_without_agent_settings_raises_not_configured_and_saves_nothing(
    setup: DiagnosisSetup,
) -> None:
    request = setup.add_request()

    with pytest.raises(NotConfiguredError):
        await setup.diagnosis_service(agent=False).diagnose(OWNER, setup.service.id, request.id)

    assert setup.diagnoses.rows == []


async def test_diagnose_other_users_service_raises_not_found(setup: DiagnosisSetup) -> None:
    request = setup.add_request()

    with pytest.raises(ServiceNotFoundError):
        await setup.diagnosis_service().diagnose(OWNER + 1, setup.service.id, request.id)


async def test_diagnose_unknown_deployment_raises_not_found(setup: DiagnosisSetup) -> None:
    with pytest.raises(DeploymentRequestNotFoundError):
        await setup.diagnosis_service().diagnose(OWNER, setup.service.id, 999)


async def test_diagnose_again_returns_saved_result_without_calling_agent(
    setup: DiagnosisSetup,
) -> None:
    request = setup.add_request()
    service = setup.diagnosis_service()
    first = await service.diagnose(OWNER, setup.service.id, request.id)

    second = await service.diagnose(OWNER, setup.service.id, request.id)

    assert second is first
    assert len(setup.agent.requests) == 1
    assert len(setup.diagnoses.rows) == 1


async def test_diagnose_with_refresh_runs_again_and_keeps_history(setup: DiagnosisSetup) -> None:
    request = setup.add_request()
    service = setup.diagnosis_service()
    first = await service.diagnose(OWNER, setup.service.id, request.id)

    second = await service.diagnose(OWNER, setup.service.id, request.id, refresh=True)

    assert second is not first
    assert len(setup.agent.requests) == 2
    assert [r.status for r in setup.diagnoses.rows] == [DiagnosisStatus.SUCCEEDED] * 2


async def test_diagnose_while_another_is_running_raises_conflict(setup: DiagnosisSetup) -> None:
    request = setup.add_request()
    setup.diagnoses.seed(request.id, DiagnosisStatus.RUNNING)

    with pytest.raises(DiagnosisInProgressError):
        await setup.diagnosis_service().diagnose(OWNER, setup.service.id, request.id)

    assert setup.agent.requests == []


async def test_diagnose_closes_stale_running_row_and_starts_again(setup: DiagnosisSetup) -> None:
    request = setup.add_request()
    stale = setup.diagnoses.seed(request.id, DiagnosisStatus.RUNNING, age=timedelta(minutes=30))

    diagnosis = await setup.diagnosis_service().diagnose(OWNER, setup.service.id, request.id)

    assert stale.status == DiagnosisStatus.FAILED
    assert stale.error_code == STALE_ERROR_CODE
    assert diagnosis.status == DiagnosisStatus.SUCCEEDED


async def test_diagnose_agent_error_marks_row_failed_with_agent_code_and_reraises(
    setup: DiagnosisSetup,
) -> None:
    request = setup.add_request()
    setup.agent.responses = [DiagnosisAgentError("failed", agent_code="MODEL_TIMEOUT")]

    with pytest.raises(DiagnosisAgentError):
        await setup.diagnosis_service().diagnose(OWNER, setup.service.id, request.id)

    row = setup.diagnoses.rows[0]
    assert row.status == DiagnosisStatus.FAILED
    assert row.error_code == "MODEL_TIMEOUT"
    assert row.result is None


async def test_diagnose_failed_row_does_not_block_next_attempt_or_count_as_cached(
    setup: DiagnosisSetup,
) -> None:
    request = setup.add_request()
    service = setup.diagnosis_service()
    setup.agent.responses = [DiagnosisAgentError("failed", agent_code="MODEL_TIMEOUT")]
    with pytest.raises(DiagnosisAgentError):
        await service.diagnose(OWNER, setup.service.id, request.id)
    setup.agent.responses = [valid_agent_result()]

    diagnosis = await service.diagnose(OWNER, setup.service.id, request.id)

    assert diagnosis.status == DiagnosisStatus.SUCCEEDED
    assert [r.status for r in setup.diagnoses.rows] == [
        DiagnosisStatus.FAILED,
        DiagnosisStatus.SUCCEEDED,
    ]


async def test_diagnose_without_runtime_logs_fails_without_calling_agent(
    setup: DiagnosisSetup,
) -> None:
    request = setup.add_request()
    setup.loki.entries = []

    with pytest.raises(DiagnosisLogsUnavailableError):
        await setup.diagnosis_service().diagnose(OWNER, setup.service.id, request.id)

    assert setup.agent.requests == []
    assert setup.diagnoses.rows[0].error_code == "DIAGNOSIS_LOGS_UNAVAILABLE"


async def test_diagnose_with_only_blank_logs_fails_without_calling_agent(
    setup: DiagnosisSetup,
) -> None:
    request = setup.add_request()
    setup.loki.entries = [make_log(1, "   "), make_log(2, "\n")]

    with pytest.raises(DiagnosisLogsUnavailableError):
        await setup.diagnosis_service().diagnose(OWNER, setup.service.id, request.id)

    assert setup.agent.requests == []


async def test_diagnose_service_without_target_has_no_logs(setup: DiagnosisSetup) -> None:
    request = setup.add_request()
    await setup.services.replace_targets(setup.service.id, set())

    with pytest.raises(DiagnosisLogsUnavailableError):
        await setup.diagnosis_service().diagnose(OWNER, setup.service.id, request.id)

    assert setup.loki.calls == []


async def test_diagnose_agent_rejecting_empty_logs_maps_to_logs_unavailable(
    setup: DiagnosisSetup,
) -> None:
    request = setup.add_request()
    setup.agent.responses = [DiagnosisAgentError("rejected", agent_code="EMPTY_LOGS")]

    with pytest.raises(DiagnosisLogsUnavailableError):
        await setup.diagnosis_service().diagnose(OWNER, setup.service.id, request.id)

    assert setup.diagnoses.rows[0].error_code == "DIAGNOSIS_LOGS_UNAVAILABLE"


async def test_diagnose_keeps_newest_logs_within_budget_and_marks_range_incomplete(
    setup: DiagnosisSetup,
) -> None:
    request = setup.add_request()
    setup.loki.entries = [make_log(i, f"line {i:04d} " + "x" * 80) for i in range(1, 201)]

    await setup.diagnosis_service().diagnose(OWNER, setup.service.id, request.id)

    data = setup.agent.requests[0]
    texts = [log["text"] for log in data["logs"]]
    assert 0 < len(texts) < 200
    assert texts[-1].startswith("line 0200")
    assert texts == sorted(texts)
    estimated = sum(len(t.encode()) + 200 for t in texts)
    assert estimated <= LOG_BUDGETS_BYTES[0]
    assert data["logRange"]["isComplete"] is False


async def test_diagnose_retries_with_smaller_budget_when_agent_says_input_too_large(
    setup: DiagnosisSetup,
) -> None:
    request = setup.add_request()
    setup.loki.entries = [make_log(i, "y" * 100) for i in range(1, 101)]
    setup.agent.responses = [
        DiagnosisAgentError("too large", agent_code="INPUT_TOO_LARGE", agent_status=422),
        valid_agent_result(),
    ]

    diagnosis = await setup.diagnosis_service().diagnose(OWNER, setup.service.id, request.id)

    assert diagnosis.status == DiagnosisStatus.SUCCEEDED
    first, second = setup.agent.requests
    assert len(second["logs"]) < len(first["logs"])
    assert second["logs"][-1]["text"] == first["logs"][-1]["text"]


async def test_diagnose_input_too_large_on_smallest_budget_fails(setup: DiagnosisSetup) -> None:
    request = setup.add_request()
    setup.agent.responses = [
        DiagnosisAgentError("too large", agent_code="INPUT_TOO_LARGE", agent_status=422)
    ]

    with pytest.raises(DiagnosisAgentError):
        await setup.diagnosis_service().diagnose(OWNER, setup.service.id, request.id)

    assert len(setup.agent.requests) == len(LOG_BUDGETS_BYTES)
    assert setup.diagnoses.rows[0].error_code == "INPUT_TOO_LARGE"


@pytest.mark.parametrize(
    "raw",
    [
        {"job_status": "succeeded", "analysis": None},
        {"job_status": "succeeded", "analysis": {"summary": "필수 필드 없음"}},
        {"job_status": "failed", "analysis": None, "error": {"code": "MODEL_ERROR"}},
        {"job_status": "unknown"},
    ],
)
async def test_diagnose_unusable_agent_result_is_not_saved_as_success(
    setup: DiagnosisSetup, raw: dict[str, Any]
) -> None:
    request = setup.add_request()
    setup.agent.responses = [raw]

    with pytest.raises(DiagnosisAgentError):
        await setup.diagnosis_service().diagnose(OWNER, setup.service.id, request.id)

    row = setup.diagnoses.rows[0]
    assert row.status == DiagnosisStatus.FAILED
    assert row.result is None


async def test_diagnose_sends_source_snapshot_of_the_build_when_bucket_is_readable(
    setup: DiagnosisSetup,
) -> None:
    request = setup.add_request()
    build = setup.add_build(request)
    setup.service.root_directory = "apps/api"

    await setup.diagnosis_service(snapshots=True).diagnose(OWNER, setup.service.id, request.id)

    source = setup.agent.requests[0]["source"]
    assert setup.snapshots.build_ids == [build.id]
    assert source["format"] == "tar.gz"
    assert source["downloadUrl"].endswith(f"snapshots/{build.id}.tar.gz?sig=x")
    assert source["commitSha"] == SOURCE_SHA
    assert source["rootDirectory"] == "apps/api"
    assert "expiresAt" in source


async def test_diagnose_omits_commit_sha_that_is_not_a_full_sha(setup: DiagnosisSetup) -> None:
    request = setup.add_request(source_sha="abc1234")
    setup.add_build(request)

    await setup.diagnosis_service(snapshots=True).diagnose(OWNER, setup.service.id, request.id)

    source = setup.agent.requests[0]["source"]
    assert "commitSha" not in source
    assert source["rootDirectory"] == "."


async def test_diagnose_upload_source_uses_archive_root_and_no_commit_sha(
    setup: DiagnosisSetup,
) -> None:
    # 업로드 스냅샷은 올린 폴더가 루트다. 서비스의 root_directory 를 따라가면 없는 폴더를 가리킨다.
    request = setup.add_request(source_sha="upload-3fa9c2d1b7e4")
    setup.add_build(request)
    setup.service.root_directory = "apps/api"

    await setup.diagnosis_service(snapshots=True).diagnose(OWNER, setup.service.id, request.id)

    source = setup.agent.requests[0]["source"]
    assert "commitSha" not in source
    assert source["rootDirectory"] == "."


async def test_diagnose_without_snapshot_upload_sends_logs_only(setup: DiagnosisSetup) -> None:
    request = setup.add_request()
    setup.add_build(request, codebuild_build_id=None)

    await setup.diagnosis_service(snapshots=True).diagnose(OWNER, setup.service.id, request.id)

    assert "source" not in setup.agent.requests[0]
    assert setup.snapshots.build_ids == []


async def test_diagnose_request_without_build_sends_logs_only(setup: DiagnosisSetup) -> None:
    request = setup.add_request()

    await setup.diagnosis_service(snapshots=True).diagnose(OWNER, setup.service.id, request.id)

    data = setup.agent.requests[0]
    assert "source" not in data
    assert "attemptId" not in data


async def test_diagnose_rollback_request_follows_source_request_to_find_snapshot(
    setup: DiagnosisSetup,
) -> None:
    original = setup.add_request(DeploymentStatus.SUCCEEDED, failure_code=None)
    original_build = setup.add_build(original)
    rollback = setup.add_request(source_deployment_request_id=original.id)
    setup.add_build(rollback, codebuild_build_id=None)

    await setup.diagnosis_service(snapshots=True).diagnose(OWNER, setup.service.id, rollback.id)

    assert setup.snapshots.build_ids == [original_build.id]


async def test_diagnose_old_build_snapshot_is_expired_so_source_is_skipped(
    setup: DiagnosisSetup,
) -> None:
    request = setup.add_request()
    build = setup.add_build(request)
    build.created_at = build.created_at - timedelta(days=2)

    await setup.diagnosis_service(snapshots=True).diagnose(OWNER, setup.service.id, request.id)

    assert "source" not in setup.agent.requests[0]


async def test_diagnose_never_sends_variables_or_secrets(setup: DiagnosisSetup) -> None:
    request = setup.add_request()
    request.variables_snapshot = {"DATABASE_URL": "gAAAAA-encrypted"}

    await setup.diagnosis_service().diagnose(OWNER, setup.service.id, request.id)

    assert "gAAAAA-encrypted" not in str(setup.agent.requests[0])


async def test_get_diagnosis_returns_latest_and_raises_when_none(setup: DiagnosisSetup) -> None:
    request = setup.add_request()
    service = setup.diagnosis_service()
    with pytest.raises(DiagnosisNotFoundError):
        await service.get_diagnosis(OWNER, setup.service.id, request.id)

    setup.agent.responses = [DiagnosisAgentError("failed", agent_code="MODEL_TIMEOUT")]
    with pytest.raises(DiagnosisAgentError):
        await service.diagnose(OWNER, setup.service.id, request.id)

    latest = await service.get_diagnosis(OWNER, setup.service.id, request.id)

    assert latest.status == DiagnosisStatus.FAILED
    assert latest.error_code == "MODEL_TIMEOUT"


async def test_get_diagnosis_does_not_need_agent_settings(setup: DiagnosisSetup) -> None:
    request = setup.add_request()
    setup.diagnoses.seed(request.id, DiagnosisStatus.SUCCEEDED, result=valid_agent_result())

    diagnosis = await setup.diagnosis_service(agent=False).get_diagnosis(
        OWNER, setup.service.id, request.id
    )

    assert diagnosis.status == DiagnosisStatus.SUCCEEDED


async def test_get_diagnosis_other_users_service_raises_not_found(setup: DiagnosisSetup) -> None:
    request = setup.add_request()

    with pytest.raises(ServiceNotFoundError):
        await setup.diagnosis_service().get_diagnosis(OWNER + 1, setup.service.id, request.id)


async def test_start_diagnosis_creates_running_row_without_calling_agent(
    setup: DiagnosisSetup,
) -> None:
    request = setup.add_request()

    started = await setup.diagnosis_service().start_diagnosis(OWNER, setup.service.id, request.id)

    assert started.is_started is True
    assert started.diagnosis.status == DiagnosisStatus.RUNNING
    assert started.diagnosis.requested_by == OWNER
    assert setup.agent.requests == []
    assert setup.session.commit_count == 1


async def test_start_diagnosis_with_saved_success_does_not_start_again(
    setup: DiagnosisSetup,
) -> None:
    request = setup.add_request()
    saved = setup.diagnoses.seed(request.id, DiagnosisStatus.SUCCEEDED, result=valid_agent_result())

    started = await setup.diagnosis_service().start_diagnosis(OWNER, setup.service.id, request.id)

    assert started.is_started is False
    assert started.diagnosis is saved
    assert len(setup.diagnoses.rows) == 1


async def test_start_diagnosis_without_agent_settings_raises_before_touching_anything(
    setup: DiagnosisSetup,
) -> None:
    with pytest.raises(NotConfiguredError):
        await setup.diagnosis_service(agent=False).start_diagnosis(OWNER, setup.service.id, 999)


async def test_run_diagnosis_unexpected_error_rolls_back_marks_internal_error_and_reraises(
    setup: DiagnosisSetup,
) -> None:
    request = setup.add_request()
    service = setup.diagnosis_service()
    started = await service.start_diagnosis(OWNER, setup.service.id, request.id)
    setup.agent.responses = [RuntimeError("boom")]

    with pytest.raises(RuntimeError):
        await service.run_diagnosis(OWNER, setup.service.id, request.id, started.diagnosis.id)

    assert setup.session.rollback_count == 1
    assert started.diagnosis.status == DiagnosisStatus.FAILED
    assert started.diagnosis.error_code == INTERNAL_ERROR_CODE


async def test_run_diagnosis_in_background_saves_result(setup: DiagnosisSetup) -> None:
    request = setup.add_request()
    started = await setup.diagnosis_service().start_diagnosis(OWNER, setup.service.id, request.id)

    await run_diagnosis_in_background(
        setup.diagnosis_service_opener(), OWNER, setup.service.id, request.id, started.diagnosis.id
    )

    assert started.diagnosis.status == DiagnosisStatus.SUCCEEDED
    assert started.diagnosis.result is not None


async def test_run_diagnosis_in_background_records_domain_error_without_raising(
    setup: DiagnosisSetup,
) -> None:
    request = setup.add_request()
    started = await setup.diagnosis_service().start_diagnosis(OWNER, setup.service.id, request.id)
    setup.agent.responses = [DiagnosisAgentError("failed", agent_code="MODEL_RATE_LIMIT")]

    await run_diagnosis_in_background(
        setup.diagnosis_service_opener(), OWNER, setup.service.id, request.id, started.diagnosis.id
    )

    assert started.diagnosis.status == DiagnosisStatus.FAILED
    assert started.diagnosis.error_code == "MODEL_RATE_LIMIT"


async def test_run_diagnosis_in_background_logs_unexpected_error_instead_of_raising(
    setup: DiagnosisSetup, caplog: pytest.LogCaptureFixture
) -> None:
    request = setup.add_request()
    started = await setup.diagnosis_service().start_diagnosis(OWNER, setup.service.id, request.id)
    setup.agent.responses = [RuntimeError("boom")]

    with caplog.at_level("ERROR"):
        await run_diagnosis_in_background(
            setup.diagnosis_service_opener(),
            OWNER,
            setup.service.id,
            request.id,
            started.diagnosis.id,
        )

    assert "diagnosis crashed" in caplog.text
    assert started.diagnosis.error_code == INTERNAL_ERROR_CODE


async def test_diagnose_build_failure_uses_stored_build_logs_not_runtime_logs(
    setup: DiagnosisSetup,
) -> None:
    request = setup.add_request(failure_code=FailureCode.BUILD_FAILED)
    setup.add_build(
        request,
        log_tail=make_log_tail(["npm ERR! missing script: build", "error Command failed"]),
    )

    diagnosis = await setup.diagnosis_service().diagnose(OWNER, setup.service.id, request.id)

    assert diagnosis.status == DiagnosisStatus.SUCCEEDED
    data = setup.agent.requests[0]
    assert data["failedStage"] == "build"
    assert [log["text"] for log in data["logs"]] == [
        "npm ERR! missing script: build",
        "error Command failed",
    ]
    assert {(log["stage"], log["sourceId"], log["stream"]) for log in data["logs"]} == {
        ("build", "codebuild", "combined")
    }
    assert [log["sequence"] for log in data["logs"]] == [1, 2]
    assert data["logRange"]["from"] <= data["logs"][0]["timestamp"]
    assert data["logs"][-1]["timestamp"] <= data["logRange"]["to"]
    assert data["logRange"]["isComplete"] is True
    assert setup.loki.calls == []


async def test_diagnose_build_failure_marks_range_incomplete_when_tail_was_cut(
    setup: DiagnosisSetup,
) -> None:
    request = setup.add_request(failure_code=FailureCode.BUILD_TIMED_OUT)
    setup.add_build(request, log_tail=make_log_tail(["step 9"], is_truncated=True))

    await setup.diagnosis_service().diagnose(OWNER, setup.service.id, request.id)

    assert setup.agent.requests[0]["logRange"]["isComplete"] is False


async def test_diagnose_build_failure_without_stored_logs_fails_without_calling_anything(
    setup: DiagnosisSetup,
) -> None:
    request = setup.add_request(failure_code=FailureCode.BUILD_FAILED)
    setup.add_build(request)

    with pytest.raises(DiagnosisLogsUnavailableError):
        await setup.diagnosis_service().diagnose(OWNER, setup.service.id, request.id)

    assert setup.agent.requests == []
    assert setup.loki.calls == []
    assert setup.diagnoses.rows[0].error_code == "DIAGNOSIS_LOGS_UNAVAILABLE"


async def test_diagnose_build_failure_without_build_row_fails(setup: DiagnosisSetup) -> None:
    request = setup.add_request(failure_code=FailureCode.SOURCE_REF_NOT_FOUND)

    with pytest.raises(DiagnosisLogsUnavailableError):
        await setup.diagnosis_service().diagnose(OWNER, setup.service.id, request.id)

    assert setup.agent.requests == []


async def test_diagnose_build_failure_skips_malformed_entries_and_fails_when_none_remain(
    setup: DiagnosisSetup,
) -> None:
    request = setup.add_request(failure_code=FailureCode.BUILD_FAILED)
    tail = make_log_tail(["ok line"])
    tail["entries"] += [{"message": "no timestamp"}, {"timestamp": "not-a-date", "message": "x"}]
    setup.add_build(request, log_tail=tail)

    await setup.diagnosis_service().diagnose(OWNER, setup.service.id, request.id)

    assert [log["text"] for log in setup.agent.requests[0]["logs"]] == ["ok line"]

    broken = setup.add_request(failure_code=FailureCode.BUILD_FAILED)
    setup.add_build(broken, log_tail={"entries": [{"message": "no timestamp"}]})
    with pytest.raises(DiagnosisLogsUnavailableError):
        await setup.diagnosis_service().diagnose(OWNER, setup.service.id, broken.id)


async def test_diagnose_build_failure_keeps_newest_build_logs_within_budget(
    setup: DiagnosisSetup,
) -> None:
    request = setup.add_request(failure_code=FailureCode.BUILD_FAILED)
    setup.add_build(
        request,
        log_tail=make_log_tail([f"line {i:04d} " + "x" * 80 for i in range(1, 201)]),
    )

    await setup.diagnosis_service().diagnose(OWNER, setup.service.id, request.id)

    data = setup.agent.requests[0]
    texts = [log["text"] for log in data["logs"]]
    assert 0 < len(texts) < 200
    assert texts[-1].startswith("line 0200")
    assert sum(len(t.encode()) + 200 for t in texts) <= LOG_BUDGETS_BYTES[0]
    assert data["logRange"]["isComplete"] is False


async def test_diagnose_build_failure_also_sends_source_snapshot(setup: DiagnosisSetup) -> None:
    request = setup.add_request(failure_code=FailureCode.BUILD_FAILED)
    build = setup.add_build(request, log_tail=make_log_tail(["Dockerfile:3 error"]))

    await setup.diagnosis_service(snapshots=True).diagnose(OWNER, setup.service.id, request.id)

    assert setup.snapshots.build_ids == [build.id]
    assert "source" in setup.agent.requests[0]


async def test_diagnose_deploy_stage_failure_ignores_stored_build_logs(
    setup: DiagnosisSetup,
) -> None:
    request = setup.add_request(failure_code=FailureCode.DEPLOY_TIMED_OUT)
    setup.add_build(request, log_tail=make_log_tail(["old build output"]))

    await setup.diagnosis_service().diagnose(OWNER, setup.service.id, request.id)

    data = setup.agent.requests[0]
    assert {log["stage"] for log in data["logs"]} == {"runtime"}
    assert [log["text"] for log in data["logs"]] == ["line 1", "line 2", "line 3"]


async def test_diagnose_build_failure_drops_noise_after_failure_marker_and_keeps_error_output(
    setup: DiagnosisSetup,
) -> None:
    """운영 E2E 에서 찾은 문제: 실패 뒤 후처리 줄이 예산을 채워 오류 출력이 밀려났다."""
    request = setup.add_request(failure_code=FailureCode.BUILD_FAILED)
    echoed_script = [
        f'cache="type=registry,ref=$IMAGE_REPO:cache" step {i} ' + "z" * 90 for i in range(17)
    ]
    setup.add_build(
        request,
        log_tail=make_log_tail(
            [f"#12 [build 3/4] RUN npm install {i}" for i in range(10)]
            + ['ERROR: failed to build: process "sh -c npm run missing" exit code: 1']
            + echoed_script
            + ["[Container] 2026/10/03 03:03:13.026439 Phase complete: BUILD State: FAILED"]
            + [
                f"[Container] 2026/10/03 03:03:13.0{i} Phase complete: POST_BUILD State: SUCCEEDED "
                + "n" * 80
                for i in range(30)
            ]
        ),
    )

    await setup.diagnosis_service().diagnose(OWNER, setup.service.id, request.id)

    texts = [log["text"] for log in setup.agent.requests[0]["logs"]]
    assert texts[-1].endswith("Phase complete: BUILD State: FAILED")
    assert any(t.startswith("ERROR: failed to build") for t in texts)
    assert not any("POST_BUILD" in t for t in texts)


async def test_diagnose_build_failure_without_failure_marker_keeps_all_lines(
    setup: DiagnosisSetup,
) -> None:
    request = setup.add_request(failure_code=FailureCode.BUILD_TIMED_OUT)
    setup.add_build(request, log_tail=make_log_tail(["step 1", "step 2", "still running"]))

    await setup.diagnosis_service().diagnose(OWNER, setup.service.id, request.id)

    assert [log["text"] for log in setup.agent.requests[0]["logs"]] == [
        "step 1",
        "step 2",
        "still running",
    ]


async def test_diagnose_build_failure_uses_last_failure_marker_when_several_exist(
    setup: DiagnosisSetup,
) -> None:
    request = setup.add_request(failure_code=FailureCode.BUILD_FAILED)
    setup.add_build(
        request,
        log_tail=make_log_tail(
            [
                "Phase complete: INSTALL State: FAILED",
                "retrying build",
                "real error output",
                "Phase complete: BUILD State: FAILED",
                "Phase complete: POST_BUILD State: SUCCEEDED",
            ]
        ),
    )

    await setup.diagnosis_service().diagnose(OWNER, setup.service.id, request.id)

    assert [log["text"] for log in setup.agent.requests[0]["logs"]] == [
        "Phase complete: INSTALL State: FAILED",
        "retrying build",
        "real error output",
        "Phase complete: BUILD State: FAILED",
    ]


async def test_repair_context_returns_exact_owned_original_diagnosis(
    setup: DiagnosisSetup,
) -> None:
    request = setup.add_request()
    build = setup.add_build(request)
    build.source_sha = request.source_sha
    raw = valid_agent_result()
    raw["preserved_extra"] = {"receipt": "original"}
    first = setup.diagnoses.seed(request.id, DiagnosisStatus.SUCCEEDED, result=raw)
    setup.diagnoses.seed(request.id, DiagnosisStatus.RUNNING)
    context = await setup.diagnosis_service(snapshots=True).get_repair_context(
        OWNER, setup.service.id, request.id, diagnosis_id=first.id
    )
    assert context["diagnosisId"] == first.id
    assert context["diagnosisResult"] == raw
    assert context["source"]["commitSha"] == request.source_sha
    assert len(setup.snapshots.build_ids) == 1
    assert "downloadUrl" not in str(first.result)
    with pytest.raises(ServiceNotFoundError):
        await setup.diagnosis_service(snapshots=True).get_repair_context(
            OWNER + 1, setup.service.id, request.id, diagnosis_id=first.id
        )


async def test_repair_context_rejects_diagnosis_from_another_deployment(
    setup: DiagnosisSetup,
) -> None:
    request = setup.add_request()
    other = setup.add_request()
    diagnosis = setup.diagnoses.seed(
        other.id, DiagnosisStatus.SUCCEEDED, result=valid_agent_result()
    )
    with pytest.raises(DiagnosisNotFoundError):
        await setup.diagnosis_service(snapshots=True).get_repair_context(
            OWNER, setup.service.id, request.id, diagnosis_id=diagnosis.id
        )


async def test_repair_context_rejects_mismatched_snapshot_before_presigning(
    setup: DiagnosisSetup,
) -> None:
    from app.core.exceptions import ConflictError

    request = setup.add_request()
    build = setup.add_build(request)
    build.source_sha = "b" * 40
    diagnosis = setup.diagnoses.seed(
        request.id, DiagnosisStatus.SUCCEEDED, result=valid_agent_result()
    )
    with pytest.raises(ConflictError):
        await setup.diagnosis_service(snapshots=True).get_repair_context(
            OWNER, setup.service.id, request.id, diagnosis_id=diagnosis.id
        )
    assert setup.snapshots.build_ids == []
