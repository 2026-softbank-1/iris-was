import pytest

from app.core.exceptions import (
    DeploymentRequestNotFoundError,
    InvalidInputError,
    InvalidStatusTransitionError,
)
from app.enums import DeploymentStatus, DeploymentTrigger, FailureCode
from app.services.deployment_status_service import ALLOWED_TRANSITIONS
from tests.fakes_deployment import OWNER, DeploymentSetup

ALLOWED_PAIRS = [(src, dst) for src, dsts in ALLOWED_TRANSITIONS.items() for dst in sorted(dsts)]
FORBIDDEN_PAIRS = [
    (src, dst)
    for src in DeploymentStatus
    for dst in DeploymentStatus
    if src != dst and dst not in ALLOWED_TRANSITIONS[src]
]


async def _queued_request(setup: DeploymentSetup) -> int:
    request = await setup.manual_service().create_deployment_request(
        OWNER, setup.service.id, trigger_type=DeploymentTrigger.MANUAL
    )
    return request.id


@pytest.fixture
async def setup() -> DeploymentSetup:
    return await DeploymentSetup().build()


def test_allowed_transitions_cover_every_status() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(DeploymentStatus)


async def test_transition_status_engine_stub_flow_leaves_history(setup: DeploymentSetup) -> None:
    request_id = await _queued_request(setup)
    status_service = setup.status_service()

    await status_service.transition_status(request_id, DeploymentStatus.BUILDING)
    await status_service.transition_status(request_id, DeploymentStatus.DEPLOYING)
    request = await status_service.transition_status(request_id, DeploymentStatus.SUCCEEDED)

    assert request.status == DeploymentStatus.SUCCEEDED
    assert request.failure_code is None
    assert [(h.from_status, h.to_status) for h in setup.histories.histories] == [
        (None, DeploymentStatus.QUEUED),
        (DeploymentStatus.QUEUED, DeploymentStatus.BUILDING),
        (DeploymentStatus.BUILDING, DeploymentStatus.DEPLOYING),
        (DeploymentStatus.DEPLOYING, DeploymentStatus.SUCCEEDED),
    ]
    detail = await setup.history_service().get_deployment_request(
        OWNER, setup.service.id, request_id
    )
    assert [stage.status for stage in detail.stages] == [
        DeploymentStatus.QUEUED,
        DeploymentStatus.BUILDING,
        DeploymentStatus.DEPLOYING,
        DeploymentStatus.SUCCEEDED,
    ]


@pytest.mark.parametrize(("from_status", "to_status"), ALLOWED_PAIRS)
async def test_transition_status_allowed_pair_changes_status_and_records_history(
    setup: DeploymentSetup, from_status: DeploymentStatus, to_status: DeploymentStatus
) -> None:
    request_id = await _queued_request(setup)
    setup.requests.requests[0].status = from_status
    failure_code = FailureCode.DEPLOY_FAILED if to_status == DeploymentStatus.FAILED else None

    request = await setup.status_service().transition_status(
        request_id, to_status, failure_code=failure_code
    )

    last = setup.histories.histories[-1]
    assert request.status == to_status
    assert (last.from_status, last.to_status, last.failure_code) == (
        from_status,
        to_status,
        failure_code,
    )


@pytest.mark.parametrize(("from_status", "to_status"), FORBIDDEN_PAIRS)
async def test_transition_status_forbidden_pair_raises_and_keeps_state(
    setup: DeploymentSetup, from_status: DeploymentStatus, to_status: DeploymentStatus
) -> None:
    request_id = await _queued_request(setup)
    setup.requests.requests[0].status = from_status
    history_count = len(setup.histories.histories)

    with pytest.raises(InvalidStatusTransitionError) as exc_info:
        await setup.status_service().transition_status(
            request_id, to_status, failure_code=FailureCode.DEPLOY_FAILED
        )

    assert exc_info.value.fields == {
        "deployment_request_id": request_id,
        "from_status": from_status,
        "to_status": to_status,
    }
    assert setup.requests.requests[0].status == from_status
    assert len(setup.histories.histories) == history_count


async def test_transition_status_same_status_is_noop(setup: DeploymentSetup) -> None:
    request_id = await _queued_request(setup)
    status_service = setup.status_service()
    await status_service.transition_status(request_id, DeploymentStatus.BUILDING)
    history_count = len(setup.histories.histories)

    request = await status_service.transition_status(request_id, DeploymentStatus.BUILDING)

    assert request.status == DeploymentStatus.BUILDING
    assert len(setup.histories.histories) == history_count


async def test_transition_status_failed_records_failure_code(setup: DeploymentSetup) -> None:
    request_id = await _queued_request(setup)

    request = await setup.status_service().transition_status(
        request_id, DeploymentStatus.FAILED, failure_code=FailureCode.BUILD_FAILED
    )

    assert request.failure_code == FailureCode.BUILD_FAILED
    assert setup.histories.histories[-1].failure_code == FailureCode.BUILD_FAILED


async def test_transition_status_failed_keeps_failure_code_after_rollback(
    setup: DeploymentSetup,
) -> None:
    request_id = await _queued_request(setup)
    status_service = setup.status_service()
    await status_service.transition_status(request_id, DeploymentStatus.BUILDING)
    await status_service.transition_status(request_id, DeploymentStatus.DEPLOYING)
    await status_service.transition_status(
        request_id, DeploymentStatus.FAILED, failure_code=FailureCode.DEPLOY_FAILED
    )

    request = await status_service.transition_status(request_id, DeploymentStatus.ROLLED_BACK)

    assert (request.status, request.failure_code) == (
        DeploymentStatus.ROLLED_BACK,
        FailureCode.DEPLOY_FAILED,
    )


async def test_transition_status_failed_without_failure_code_raises(
    setup: DeploymentSetup,
) -> None:
    request_id = await _queued_request(setup)

    with pytest.raises(InvalidInputError):
        await setup.status_service().transition_status(request_id, DeploymentStatus.FAILED)

    assert setup.requests.requests[0].status == DeploymentStatus.QUEUED


async def test_transition_status_failure_code_on_other_status_raises(
    setup: DeploymentSetup,
) -> None:
    request_id = await _queued_request(setup)

    with pytest.raises(InvalidInputError):
        await setup.status_service().transition_status(
            request_id, DeploymentStatus.BUILDING, failure_code=FailureCode.BUILD_FAILED
        )

    assert setup.requests.requests[0].status == DeploymentStatus.QUEUED


async def test_transition_status_unknown_request_raises_not_found(setup: DeploymentSetup) -> None:
    with pytest.raises(DeploymentRequestNotFoundError):
        await setup.status_service().transition_status(999, DeploymentStatus.BUILDING)
