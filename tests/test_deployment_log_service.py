from datetime import UTC, datetime, timedelta

import pytest

from app.clients.aws_clients import BuildLogChunk, LogLine
from app.core.exceptions import InvalidInputError, NotConfiguredError, ServiceNotFoundError
from app.enums import BuildStatus, DeploymentStatus, DeploymentTrigger, FailureCode
from app.services.deployment_log_service import BuildLogScope, LogScope
from tests.fakes_deployment import OWNER, DeploymentSetup


@pytest.fixture
async def setup() -> DeploymentSetup:
    return await DeploymentSetup().build()


def _ns(value: datetime) -> int:
    return int(value.timestamp() * 1e9)


def _entered_at(setup: DeploymentSetup, request_id: int, status: DeploymentStatus) -> datetime:
    return next(
        h.created_at
        for h in setup.histories.histories
        if h.deployment_request_id == request_id and h.to_status == status
    )


async def test_build_log_scope_of_own_build_returns_codebuild_stream(
    setup: DeploymentSetup,
) -> None:
    request = await setup.create_succeeded_request()
    build = setup.builds.builds[0]
    build.codebuild_build_id = "iris-dev-build:7c1e0f2a-uuid"
    build.status = BuildStatus.BUILDING

    scope = await setup.log_service().get_build_log_scope(OWNER, setup.service.id, request.id)

    assert scope == BuildLogScope("7c1e0f2a-uuid", request.id, BuildStatus.BUILDING, False)


async def test_build_log_scope_of_restart_follows_original_build(setup: DeploymentSetup) -> None:
    original = await setup.create_succeeded_request()
    build = setup.builds.builds[0]
    build.codebuild_build_id = "iris-dev-build:original-uuid"
    build.image_repository = "123.dkr.ecr.ap-northeast-2.amazonaws.com/iris/services/1"
    build.succeed("sha256:" + "b" * 64)
    restart = await setup.manual_service().create_deployment_request(
        OWNER, setup.service.id, trigger_type=DeploymentTrigger.RESTART
    )

    scope = await setup.log_service().get_build_log_scope(OWNER, setup.service.id, restart.id)

    assert scope == BuildLogScope("original-uuid", original.id, BuildStatus.SUCCEEDED, True)


async def test_build_log_scope_before_codebuild_starts_has_no_stream(
    setup: DeploymentSetup,
) -> None:
    request = await setup.manual_service().create_deployment_request(
        OWNER, setup.service.id, trigger_type=DeploymentTrigger.MANUAL
    )

    scope = await setup.log_service().get_build_log_scope(OWNER, setup.service.id, request.id)

    assert scope == BuildLogScope(None, None, BuildStatus.PENDING, False)


async def test_build_log_scope_of_failed_build_without_codebuild_is_finished(
    setup: DeploymentSetup,
) -> None:
    request = await setup.manual_service().create_deployment_request(
        OWNER, setup.service.id, trigger_type=DeploymentTrigger.MANUAL
    )
    setup.builds.builds[0].fail(FailureCode.SOURCE_NOT_ACCESSIBLE)

    scope = await setup.log_service().get_build_log_scope(OWNER, setup.service.id, request.id)

    assert scope == BuildLogScope(None, None, BuildStatus.FAILED, True)


async def test_build_log_scope_of_other_users_service_raises_not_found(
    setup: DeploymentSetup,
) -> None:
    request = await setup.create_succeeded_request()

    with pytest.raises(ServiceNotFoundError):
        await setup.log_service().get_build_log_scope(OWNER + 1, setup.service.id, request.id)


async def test_search_build_logs_reads_stream_and_keeps_polling_while_building(
    setup: DeploymentSetup,
) -> None:
    events = [LogLine(1_000, "step 1"), LogLine(2_000, "step 2")]
    setup.build_log_reader.read_events.return_value = BuildLogChunk(events, "f/next")
    scope = BuildLogScope("uuid", 1, BuildStatus.BUILDING, False)

    page = await setup.log_service().search_build_logs(scope, "f/prev", 500)

    setup.build_log_reader.read_events.assert_awaited_once_with(
        "/aws/codebuild/x", "uuid", 500, "f/prev"
    )
    assert page.entries == events
    assert page.next_cursor == "f/next"
    assert page.is_complete is False


async def test_search_build_logs_finished_build_is_complete_only_when_nothing_is_left(
    setup: DeploymentSetup,
) -> None:
    scope = BuildLogScope("uuid", 1, BuildStatus.SUCCEEDED, True)
    setup.build_log_reader.read_events.return_value = BuildLogChunk([LogLine(1, "x")], "f/2")

    last_page = await setup.log_service().search_build_logs(scope, None, 500)
    setup.build_log_reader.read_events.return_value = BuildLogChunk([], "f/2")
    empty_page = await setup.log_service().search_build_logs(scope, "f/2", 500)

    assert last_page.is_complete is False
    assert empty_page.is_complete is True
    assert empty_page.next_cursor == "f/2"


async def test_search_build_logs_without_stream_returns_empty_without_calling_aws(
    setup: DeploymentSetup,
) -> None:
    page = await setup.log_service(has_build_log_reader=False).search_build_logs(
        BuildLogScope(None, None, None, True), "cursor", 500
    )

    assert page.entries == []
    assert page.next_cursor == "cursor"
    assert page.is_complete is True
    setup.build_log_reader.read_events.assert_not_awaited()


@pytest.mark.parametrize("has_reader,group", [(False, "/aws/codebuild/x"), (True, None)])
async def test_search_build_logs_not_configured_raises(
    setup: DeploymentSetup, has_reader: bool, group: str | None
) -> None:
    service = setup.log_service(has_build_log_reader=has_reader, build_log_group=group)

    with pytest.raises(NotConfiguredError):
        await service.search_build_logs(
            BuildLogScope("uuid", 1, BuildStatus.BUILDING, False), None, 10
        )


async def test_deploy_log_scope_filters_by_release_and_defaults_to_deployment_period(
    setup: DeploymentSetup,
) -> None:
    request = await setup.create_succeeded_request()
    release = setup.add_release(request, target_id=1)
    started_at = _entered_at(setup, request.id, DeploymentStatus.DEPLOYING)

    scope = await setup.log_service().get_deploy_log_scope(
        OWNER, setup.service.id, request.id, None, None, None
    )

    assert scope.namespace == f"svc-{setup.service.id}"
    assert scope.target_id == 1
    assert scope.release_ids == [release.id]
    assert scope.start == started_at
    assert scope.end is not None and started_at < scope.end <= datetime.now(UTC)


async def test_search_deploy_logs_passes_release_ids_and_resolved_range(
    setup: DeploymentSetup,
) -> None:
    request = await setup.create_succeeded_request()
    release = setup.add_release(request)
    service = setup.log_service()
    scope = await service.get_deploy_log_scope(
        OWNER, setup.service.id, request.id, None, None, None
    )
    assert scope.start is not None and scope.end is not None
    setup.observability.search_logs.return_value = ["entry"]

    entries = await service.search_deploy_logs(scope, 200, "error")

    assert entries == ["entry"]
    setup.observability.search_logs.assert_awaited_once_with(
        1, scope.namespace, _ns(scope.start), _ns(scope.end), 200, "error", [release.id]
    )


async def test_deploy_log_scope_without_release_has_nothing_to_query(
    setup: DeploymentSetup,
) -> None:
    request = await setup.manual_service().create_deployment_request(
        OWNER, setup.service.id, trigger_type=DeploymentTrigger.MANUAL
    )
    service = setup.log_service()

    scope = await service.get_deploy_log_scope(
        OWNER, setup.service.id, request.id, None, None, None
    )
    entries = await service.search_deploy_logs(scope, 200, "")

    assert scope.query is None
    assert (scope.start, scope.end) == (None, None)
    assert entries == []
    setup.observability.search_logs.assert_not_awaited()


async def test_deploy_log_scope_of_replaced_deployment_ends_when_replaced(
    setup: DeploymentSetup,
) -> None:
    first = await setup.create_succeeded_request()
    setup.add_release(first)
    second = await setup.create_succeeded_request()

    scope = await setup.log_service().get_deploy_log_scope(
        OWNER, setup.service.id, first.id, None, None, None
    )

    assert scope.end == _entered_at(setup, second.id, DeploymentStatus.SUCCEEDED)


async def test_deploy_log_scope_rejects_target_the_deployment_did_not_use(
    setup: DeploymentSetup,
) -> None:
    request = await setup.create_succeeded_request()
    setup.add_release(request, target_id=1)

    with pytest.raises(InvalidInputError):
        await setup.log_service().get_deploy_log_scope(
            OWNER, setup.service.id, request.id, 2, None, None
        )


async def test_deploy_log_scope_uses_requested_range(setup: DeploymentSetup) -> None:
    request = await setup.create_succeeded_request()
    setup.add_release(request)
    end = datetime.now(UTC) - timedelta(hours=1)
    start = end - timedelta(hours=2)

    scope = await setup.log_service().get_deploy_log_scope(
        OWNER, setup.service.id, request.id, None, start, end
    )

    assert (scope.start, scope.end) == (start, end)


@pytest.mark.parametrize(
    "start,end",
    [
        (None, datetime.now(UTC) + timedelta(hours=1)),
        (datetime(2026, 1, 1), None),
        (datetime.now(UTC) - timedelta(days=9), datetime.now(UTC) - timedelta(hours=1)),
    ],
)
async def test_deploy_log_scope_rejects_invalid_range(
    setup: DeploymentSetup, start: datetime | None, end: datetime | None
) -> None:
    request = await setup.create_succeeded_request()
    setup.add_release(request)

    with pytest.raises(InvalidInputError):
        await setup.log_service().get_deploy_log_scope(
            OWNER, setup.service.id, request.id, None, start, end
        )


async def test_network_log_scope_starts_when_deployment_succeeded(setup: DeploymentSetup) -> None:
    request = await setup.create_succeeded_request()
    setup.add_release(request)
    service = setup.log_service()
    setup.observability.search_network_logs.return_value = ["entry"]

    scope = await service.get_network_log_scope(
        OWNER, setup.service.id, request.id, None, None, None
    )
    entries = await service.search_network_logs(scope, 50, "5xx")

    assert scope.start == _entered_at(setup, request.id, DeploymentStatus.SUCCEEDED)
    assert scope.end is not None
    assert entries == ["entry"]
    setup.observability.search_network_logs.assert_awaited_once_with(
        1, scope.namespace, _ns(scope.start), _ns(scope.end), 50, "5xx"
    )


async def test_network_log_scope_of_never_succeeded_deployment_has_nothing_to_query(
    setup: DeploymentSetup,
) -> None:
    request = await setup.manual_service().create_deployment_request(
        OWNER, setup.service.id, trigger_type=DeploymentTrigger.MANUAL
    )
    setup.services.targets[setup.service.id] = {1}
    service = setup.log_service()

    scope = await service.get_network_log_scope(
        OWNER, setup.service.id, request.id, None, None, None
    )
    entries = await service.search_network_logs(scope, 50, None)

    assert scope == LogScope(f"svc-{setup.service.id}", 1, [], None, None)
    assert entries == []
    setup.observability.search_network_logs.assert_not_awaited()
