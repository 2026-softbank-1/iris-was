from datetime import UTC, datetime, timedelta

import pytest

from app.core.exceptions import DeploymentRequestNotFoundError, ServiceNotFoundError
from app.enums import (
    DeploymentStatus,
    DeploymentTrigger,
    Environment,
    FailureCode,
    ReleaseStatus,
)
from app.models.deployment_request import DeploymentRequest
from app.models.deployment_status_history import DeploymentStatusHistory
from app.models.release import Release
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


async def _succeed_new_request(setup: DeploymentSetup) -> DeploymentRequest:
    request = await setup.manual_service().create_deployment_request(
        OWNER, setup.service.id, trigger_type=DeploymentTrigger.MANUAL
    )
    status_service = setup.status_service()
    await status_service.transition_status(request.id, DeploymentStatus.BUILDING)
    await status_service.transition_status(request.id, DeploymentStatus.DEPLOYING)
    await status_service.transition_status(request.id, DeploymentStatus.SUCCEEDED)
    return request


async def test_get_deployment_request_without_release_returns_service_targets(
    setup: DeploymentSetup,
) -> None:
    setup.services.targets[setup.service.id] = {1}
    request = await setup.manual_service().create_deployment_request(
        OWNER, setup.service.id, trigger_type=DeploymentTrigger.MANUAL
    )

    detail = await setup.history_service().get_deployment_request(
        OWNER, setup.service.id, request.id
    )

    assert detail.service.id == setup.service.id
    assert detail.build is not None
    assert detail.releases == []
    assert [t.name for t in detail.targets] == ["aws"]


async def test_get_deployment_request_with_release_returns_released_targets(
    setup: DeploymentSetup,
) -> None:
    setup.services.targets[setup.service.id] = {1, 2}
    request = await _succeed_new_request(setup)
    release = Release(
        deployment_request_id=request.id,
        build_id=1,
        service_id=setup.service.id,
        environment=Environment.PROD,
        target_id=2,
        image_digest="sha256:abc",
        status=ReleaseStatus.SUCCEEDED,
    )
    release.id = 7
    setup.releases.releases.append(release)

    detail = await setup.history_service().get_deployment_request(
        OWNER, setup.service.id, request.id
    )

    assert [r.id for r in detail.releases] == [7]
    assert [t.name for t in detail.targets] == ["onprem"]


async def test_get_deployment_request_replaced_by_next_succeeded_returns_replacement(
    setup: DeploymentSetup,
) -> None:
    first = await _succeed_new_request(setup)
    second = await _succeed_new_request(setup)

    first_detail = await setup.history_service().get_deployment_request(
        OWNER, setup.service.id, first.id
    )
    second_detail = await setup.history_service().get_deployment_request(
        OWNER, setup.service.id, second.id
    )

    assert first_detail.replaced_by is not None
    assert first_detail.replaced_by.deployment_request_id == second.id
    second_succeeded_at = next(
        h.created_at
        for h in setup.histories.histories
        if h.deployment_request_id == second.id and h.to_status == DeploymentStatus.SUCCEEDED
    )
    assert first_detail.replaced_by.at == second_succeeded_at
    assert second_detail.replaced_by is None


async def test_get_deployment_request_not_succeeded_has_no_replacement(
    setup: DeploymentSetup,
) -> None:
    failed = await setup.manual_service().create_deployment_request(
        OWNER, setup.service.id, trigger_type=DeploymentTrigger.MANUAL
    )
    await setup.status_service().transition_status(
        failed.id, DeploymentStatus.FAILED, failure_code=FailureCode.BUILD_FAILED
    )
    await _succeed_new_request(setup)

    detail = await setup.history_service().get_deployment_request(
        OWNER, setup.service.id, failed.id
    )

    assert detail.replaced_by is None
