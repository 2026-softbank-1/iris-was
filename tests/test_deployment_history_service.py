from datetime import UTC, datetime, timedelta

import pytest

from app.core.exceptions import DeploymentRequestNotFoundError, ServiceNotFoundError
from app.enums import DeploymentStatus, DeploymentTrigger
from app.models.deployment_request import DeploymentRequest
from app.models.deployment_status_history import DeploymentStatusHistory
from app.services.deployment_history_service import DeploymentStage, build_stages
from tests.fakes_deployment import OWNER, DeploymentSetup

T0 = datetime(2026, 10, 2, 9, 0, 0, tzinfo=UTC)


def _history(
    from_status: DeploymentStatus | None, to_status: DeploymentStatus, seconds: int
) -> DeploymentStatusHistory:
    return DeploymentStatusHistory(
        from_status=from_status, to_status=to_status, created_at=T0 + timedelta(seconds=seconds)
    )


def _request(status: DeploymentStatus) -> DeploymentRequest:
    return DeploymentRequest(status=status, created_at=T0, updated_at=T0)


def test_build_stages_splits_history_into_consecutive_stages() -> None:
    histories = [
        _history(None, DeploymentStatus.QUEUED, 0),
        _history(DeploymentStatus.QUEUED, DeploymentStatus.BUILDING, 5),
        _history(DeploymentStatus.BUILDING, DeploymentStatus.DEPLOYING, 65),
        _history(DeploymentStatus.DEPLOYING, DeploymentStatus.SUCCEEDED, 95),
    ]

    stages = build_stages(_request(DeploymentStatus.SUCCEEDED), histories)

    assert stages == [
        DeploymentStage(DeploymentStatus.QUEUED, T0, T0 + timedelta(seconds=5)),
        DeploymentStage(
            DeploymentStatus.BUILDING, T0 + timedelta(seconds=5), T0 + timedelta(seconds=65)
        ),
        DeploymentStage(
            DeploymentStatus.DEPLOYING, T0 + timedelta(seconds=65), T0 + timedelta(seconds=95)
        ),
        DeploymentStage(DeploymentStatus.SUCCEEDED, T0 + timedelta(seconds=95), None),
    ]


def test_build_stages_in_progress_request_leaves_last_stage_open() -> None:
    histories = [
        _history(None, DeploymentStatus.QUEUED, 0),
        _history(DeploymentStatus.QUEUED, DeploymentStatus.BUILDING, 5),
    ]

    stages = build_stages(_request(DeploymentStatus.BUILDING), histories)

    assert stages[-1] == DeploymentStage(DeploymentStatus.BUILDING, T0 + timedelta(seconds=5), None)


def test_build_stages_without_history_uses_created_at_and_current_status() -> None:
    stages = build_stages(_request(DeploymentStatus.BUILDING), [])

    assert stages == [DeploymentStage(DeploymentStatus.BUILDING, T0, None)]


@pytest.fixture
async def setup() -> DeploymentSetup:
    return await DeploymentSetup().build()


async def test_search_deployment_requests_returns_newest_first_with_total(
    setup: DeploymentSetup,
) -> None:
    manual = setup.manual_service()
    ids = []
    for _ in range(3):
        request = await manual.create_deployment_request(
            OWNER, setup.service.id, trigger_type=DeploymentTrigger.MANUAL
        )
        ids.append(request.id)
        setup.finish_active_requests()

    page = await setup.history_service().search_deployment_requests(
        OWNER, setup.service.id, page=0, size=2
    )

    assert [r.id for r in page.items] == [ids[2], ids[1]]
    assert page.total == 3


async def test_search_deployment_requests_second_page_returns_rest(setup: DeploymentSetup) -> None:
    manual = setup.manual_service()
    for _ in range(3):
        await manual.create_deployment_request(
            OWNER, setup.service.id, trigger_type=DeploymentTrigger.MANUAL
        )
        setup.finish_active_requests()

    page = await setup.history_service().search_deployment_requests(
        OWNER, setup.service.id, page=1, size=2
    )

    assert [r.id for r in page.items] == [1]


async def test_search_deployment_requests_other_users_service_raises_not_found(
    setup: DeploymentSetup,
) -> None:
    with pytest.raises(ServiceNotFoundError):
        await setup.history_service().search_deployment_requests(
            OWNER + 1, setup.service.id, page=0, size=20
        )


async def test_get_deployment_request_returns_history_and_stages(setup: DeploymentSetup) -> None:
    request = await setup.manual_service().create_deployment_request(
        OWNER, setup.service.id, trigger_type=DeploymentTrigger.MANUAL
    )
    await setup.status_service().transition_status(request.id, DeploymentStatus.BUILDING)

    detail = await setup.history_service().get_deployment_request(
        OWNER, setup.service.id, request.id
    )

    assert detail.deployment_request.id == request.id
    assert [h.to_status for h in detail.histories] == [
        DeploymentStatus.QUEUED,
        DeploymentStatus.BUILDING,
    ]
    assert [s.status for s in detail.stages] == [DeploymentStatus.QUEUED, DeploymentStatus.BUILDING]
    assert detail.stages[0].finished_at == detail.stages[1].started_at
    assert detail.stages[1].finished_at is None


async def test_get_deployment_request_of_other_service_raises_not_found(
    setup: DeploymentSetup,
) -> None:
    with pytest.raises(DeploymentRequestNotFoundError):
        await setup.history_service().get_deployment_request(OWNER, setup.service.id, 999)
