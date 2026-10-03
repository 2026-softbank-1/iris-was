import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import pytest

from app.core.exceptions import DiagnosisAgentError, NotConfiguredError
from app.enums import DeploymentStatus, DeploymentTrigger, DiagnosisStatus
from app.services.auto_diagnosis import AutoDiagnosisRunner
from app.services.diagnosis_service import (
    AUTO_MAX_AGE,
    STALE_AFTER,
    STALE_ERROR_CODE,
    AutomaticDiagnosis,
    DiagnosisService,
)
from tests.fakes_diagnosis import OWNER, DiagnosisSetup


@pytest.fixture
async def setup() -> DiagnosisSetup:
    return await DiagnosisSetup().build()


async def _wait_until(condition: Callable[[], bool]) -> None:
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not met within 2 seconds")


async def test_start_next_automatic_diagnosis_starts_failed_deployment_without_user(
    setup: DiagnosisSetup,
) -> None:
    request = setup.add_request()

    started = await setup.diagnosis_service().start_next_automatic_diagnosis()

    assert started == AutomaticDiagnosis(OWNER, setup.service.id, request.id, 1)
    (row,) = setup.diagnoses.rows
    assert (row.status, row.requested_by) == (DiagnosisStatus.RUNNING, None)
    assert setup.agent.requests == []


@pytest.mark.parametrize(
    "status",
    [
        DeploymentStatus.FAILED,
        DeploymentStatus.ROLLED_BACK,
        DeploymentStatus.MANUAL_INTERVENTION,
    ],
)
async def test_start_next_automatic_diagnosis_picks_every_confirmed_failure_status(
    setup: DiagnosisSetup, status: DeploymentStatus
) -> None:
    request = setup.add_request(status)

    started = await setup.diagnosis_service().start_next_automatic_diagnosis()

    assert started is not None and started.deployment_request_id == request.id


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
async def test_start_next_automatic_diagnosis_skips_deployments_that_have_not_failed(
    setup: DiagnosisSetup, status: DeploymentStatus
) -> None:
    setup.add_request(status, failure_code=None)

    assert await setup.diagnosis_service().start_next_automatic_diagnosis() is None
    assert setup.diagnoses.rows == []


async def test_start_next_automatic_diagnosis_skips_remove_requests(
    setup: DiagnosisSetup,
) -> None:
    setup.add_request(trigger_type=DeploymentTrigger.REMOVE)

    assert await setup.diagnosis_service().start_next_automatic_diagnosis() is None


@pytest.mark.parametrize(
    "trigger_type",
    [
        DeploymentTrigger.MANUAL,
        DeploymentTrigger.PUSH,
        DeploymentTrigger.CLI,
        DeploymentTrigger.REDEPLOY,
        DeploymentTrigger.ROLLBACK,
        DeploymentTrigger.RESTART,
    ],
)
async def test_start_next_automatic_diagnosis_diagnoses_every_trigger_except_remove(
    setup: DiagnosisSetup, trigger_type: DeploymentTrigger
) -> None:
    setup.add_request(trigger_type=trigger_type)

    assert await setup.diagnosis_service().start_next_automatic_diagnosis() is not None


@pytest.mark.parametrize(
    ("status", "age"),
    [
        (DiagnosisStatus.SUCCEEDED, timedelta(minutes=2)),
        (DiagnosisStatus.FAILED, timedelta(minutes=2)),
        (DiagnosisStatus.RUNNING, timedelta(minutes=1)),
    ],
)
async def test_start_next_automatic_diagnosis_does_not_repeat_a_diagnosed_deployment(
    setup: DiagnosisSetup, status: DiagnosisStatus, age: timedelta
) -> None:
    request = setup.add_request()
    setup.diagnoses.seed(request.id, status, age=age)

    assert await setup.diagnosis_service().start_next_automatic_diagnosis() is None
    assert len(setup.diagnoses.rows) == 1


async def test_start_next_automatic_diagnosis_restarts_a_diagnosis_left_running_by_a_dead_server(
    setup: DiagnosisSetup,
) -> None:
    request = setup.add_request()
    stale = setup.diagnoses.seed(
        request.id, DiagnosisStatus.RUNNING, age=STALE_AFTER + timedelta(minutes=1)
    )

    started = await setup.diagnosis_service().start_next_automatic_diagnosis()

    assert started is not None and started.deployment_request_id == request.id
    assert (stale.status, stale.error_code) == (DiagnosisStatus.FAILED, STALE_ERROR_CODE)
    running = [r for r in setup.diagnoses.rows if r.status == DiagnosisStatus.RUNNING]
    assert [r.id for r in running] == [started.diagnosis_id]


async def test_start_next_automatic_diagnosis_leaves_failures_older_than_the_window_alone(
    setup: DiagnosisSetup,
) -> None:
    setup.add_request(failed_ago=AUTO_MAX_AGE + timedelta(minutes=1))

    assert await setup.diagnosis_service().start_next_automatic_diagnosis() is None


async def test_start_next_automatic_diagnosis_takes_the_longest_waiting_failure_first(
    setup: DiagnosisSetup,
) -> None:
    setup.add_request(failed_ago=timedelta(minutes=3))
    older = setup.add_request(failed_ago=timedelta(minutes=8))

    started = await setup.diagnosis_service().start_next_automatic_diagnosis()

    assert started is not None and started.deployment_request_id == older.id


async def test_start_next_automatic_diagnosis_runs_one_at_a_time_across_all_diagnoses(
    setup: DiagnosisSetup,
) -> None:
    busy = setup.add_request(failed_ago=timedelta(minutes=8))
    waiting = setup.add_request(failed_ago=timedelta(minutes=3))
    running = setup.diagnoses.seed(busy.id, DiagnosisStatus.RUNNING, age=timedelta(seconds=10))
    service = setup.diagnosis_service()

    assert await service.start_next_automatic_diagnosis() is None

    running.succeed({})
    started = await service.start_next_automatic_diagnosis()
    assert started is not None and started.deployment_request_id == waiting.id


async def test_start_next_automatic_diagnosis_ignores_a_stale_running_row_for_the_limit(
    setup: DiagnosisSetup,
) -> None:
    other = setup.add_request(failed_ago=timedelta(minutes=9))
    setup.diagnoses.seed(other.id, DiagnosisStatus.RUNNING, age=STALE_AFTER + timedelta(minutes=1))
    # 낡은 행은 진행 중으로 세지 않지만, 그 배포 자신은 다시 시작할 후보다.
    started = await setup.diagnosis_service().start_next_automatic_diagnosis()

    assert started is not None and started.deployment_request_id == other.id


async def test_start_next_automatic_diagnosis_skips_deleted_service(
    setup: DiagnosisSetup,
) -> None:
    setup.add_request()
    setup.service.mark_as_deleted()

    assert await setup.diagnosis_service().start_next_automatic_diagnosis() is None


async def test_start_next_automatic_diagnosis_without_agent_raises_not_configured(
    setup: DiagnosisSetup,
) -> None:
    setup.add_request()

    with pytest.raises(NotConfiguredError):
        await setup.diagnosis_service(agent=False).start_next_automatic_diagnosis()
    assert setup.diagnoses.rows == []


async def test_start_next_automatic_diagnosis_yields_when_another_server_started_it_first(
    setup: DiagnosisSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup.add_request()

    async def lose_the_race(deployment_request_id: int, requested_by: int | None) -> None:
        return None

    monkeypatch.setattr(setup.diagnoses, "add_running_if_absent", lose_the_race)

    assert await setup.diagnosis_service().start_next_automatic_diagnosis() is None


async def test_run_once_diagnoses_a_failure_and_does_not_repeat_it(setup: DiagnosisSetup) -> None:
    request = setup.add_request()
    runner = AutoDiagnosisRunner(setup.diagnosis_service_opener(), 0.01)

    assert await runner.run_once() is True

    (row,) = setup.diagnoses.rows
    assert (row.status, row.requested_by) == (DiagnosisStatus.SUCCEEDED, None)
    assert [data["deploymentId"] for data in setup.agent.requests] == [request.id]
    assert await runner.run_once() is False
    assert len(setup.agent.requests) == 1


async def test_run_once_without_failures_does_nothing(setup: DiagnosisSetup) -> None:
    setup.add_request(DeploymentStatus.SUCCEEDED, failure_code=None)

    assert await AutoDiagnosisRunner(setup.diagnosis_service_opener(), 0.01).run_once() is False
    assert setup.agent.requests == []


async def test_run_once_does_not_retry_a_failed_diagnosis_by_itself(
    setup: DiagnosisSetup,
) -> None:
    setup.add_request()
    setup.agent.responses = [DiagnosisAgentError("busy", agent_code="BUSY")]
    runner = AutoDiagnosisRunner(setup.diagnosis_service_opener(), 0.01)

    assert await runner.run_once() is True
    assert await runner.run_once() is False

    (row,) = setup.diagnoses.rows
    assert (row.status, row.error_code) == (DiagnosisStatus.FAILED, "BUSY")
    assert len(setup.agent.requests) == 1


async def test_runner_keeps_diagnosing_new_failures_until_stopped(setup: DiagnosisSetup) -> None:
    runner = AutoDiagnosisRunner(setup.diagnosis_service_opener(), 0.01)
    runner.start()
    try:
        first = setup.add_request()
        await _wait_until(lambda: len(setup.diagnoses.rows) == 1 and _all_finished(setup))
        second = setup.add_request()
        await _wait_until(lambda: len(setup.diagnoses.rows) == 2 and _all_finished(setup))
    finally:
        await runner.stop()

    assert [data["deploymentId"] for data in setup.agent.requests] == [first.id, second.id]


async def test_runner_survives_an_iteration_that_raises(setup: DiagnosisSetup) -> None:
    setup.add_request()
    real_opener = setup.diagnosis_service_opener()
    opened = 0

    @asynccontextmanager
    async def flaky_opener() -> AsyncIterator[DiagnosisService]:
        nonlocal opened
        opened += 1
        if opened == 1:
            raise RuntimeError("database is down")
        async with real_opener() as service:
            yield service

    runner = AutoDiagnosisRunner(flaky_opener, 0.01)
    runner.start()
    try:
        await _wait_until(lambda: _all_finished(setup) and len(setup.diagnoses.rows) == 1)
    finally:
        await runner.stop()

    assert opened >= 2


async def test_runner_stop_cancels_a_diagnosis_that_outlasts_the_grace_period(
    setup: DiagnosisSetup,
) -> None:
    setup.add_request()
    started = asyncio.Event()

    class HangingAgent:
        async def diagnose(self, data: dict[str, Any]) -> dict[str, Any]:
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    setup.agent = HangingAgent()  # type: ignore[assignment]
    runner = AutoDiagnosisRunner(setup.diagnosis_service_opener(), 0.01)
    runner.start()
    async with asyncio.timeout(2):
        await started.wait()

    async with asyncio.timeout(2):
        await runner.stop(grace_seconds=0.05)

    # 끝나지 못한 진행 중 행은 남고, 다른 서버가 낡은 행으로 보고 다시 시작한다.
    (row,) = setup.diagnoses.rows
    assert row.status == DiagnosisStatus.RUNNING


def _all_finished(setup: DiagnosisSetup) -> bool:
    return all(r.status != DiagnosisStatus.RUNNING for r in setup.diagnoses.rows)
