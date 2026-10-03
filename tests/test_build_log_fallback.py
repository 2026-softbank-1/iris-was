"""CloudWatch 를 읽을 수 없는 환경에서는 Build Worker 가 남긴 `builds.log_tail` 을 보여 준다."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.clients.aws_clients import BuildLogChunk, LogLine
from app.core.exceptions import NotConfiguredError
from app.dependencies import get_current_user, get_deployment_log_service, get_session
from app.enums import DeploymentTrigger, FailureCode
from app.main import app
from app.models.user import User
from app.services.deployment_log_service import BuildLogScope
from tests.fakes_deployment import OWNER, DeploymentSetup

FAILED_AT = datetime(2026, 10, 2, 14, 29, 34, 920000, tzinfo=UTC)
FAILED_AT_MS = int(FAILED_AT.timestamp() * 1000)
STORED_TAIL = {
    "entries": [
        {"timestamp": "2026-10-02T14:29:34.920Z", "message": "npm ERR! missing script: build"},
        {"timestamp": "not a time", "message": "skipped"},
        {"message": "skipped too"},
    ],
    "is_truncated": True,
}


@pytest.fixture
async def setup() -> DeploymentSetup:
    return await DeploymentSetup().build()


async def _failed_request_with_tail(setup: DeploymentSetup) -> int:
    request = await setup.manual_service().create_deployment_request(
        OWNER, setup.service.id, trigger_type=DeploymentTrigger.MANUAL
    )
    build = setup.builds.builds[0]
    build.codebuild_build_id = "iris-dev-build:uuid-1"
    build.record_log_tail(STORED_TAIL)
    build.fail(FailureCode.BUILD_FAILED)
    return request.id


async def test_build_log_scope_reads_stored_tail_and_skips_malformed_entries(
    setup: DeploymentSetup,
) -> None:
    request_id = await _failed_request_with_tail(setup)

    scope = await setup.log_service().get_build_log_scope(OWNER, setup.service.id, request_id)

    assert scope.stored_tail == [LogLine(FAILED_AT_MS, "npm ERR! missing script: build")]
    assert scope.is_tail_truncated is True


async def test_build_log_scope_without_stored_tail_has_empty_tail(setup: DeploymentSetup) -> None:
    request = await setup.create_succeeded_request()
    setup.builds.builds[0].codebuild_build_id = "iris-dev-build:uuid-1"

    scope = await setup.log_service().get_build_log_scope(OWNER, setup.service.id, request.id)

    assert (scope.stored_tail, scope.is_tail_truncated) == ([], False)


@pytest.mark.parametrize("is_truncated", [True, False])
async def test_search_build_logs_without_reader_returns_stored_tail(
    setup: DeploymentSetup, is_truncated: bool
) -> None:
    tail = [LogLine(1_000, "npm ERR!")]
    scope = BuildLogScope("uuid", 7, None, True, tail, is_truncated)

    page = await setup.log_service(has_build_log_reader=False).search_build_logs(scope, None, 500)

    assert page.entries == tail
    assert page.next_cursor is None
    assert page.is_complete is True
    assert page.is_partial is is_truncated
    setup.build_log_reader.read_events.assert_not_awaited()


async def test_search_build_logs_without_group_returns_stored_tail(setup: DeploymentSetup) -> None:
    scope = BuildLogScope("uuid", 7, None, True, [LogLine(1_000, "npm ERR!")], False)

    page = await setup.log_service(build_log_group=None).search_build_logs(scope, None, 500)

    assert [line.message for line in page.entries] == ["npm ERR!"]


async def test_search_build_logs_without_reader_and_tail_raises_not_configured(
    setup: DeploymentSetup,
) -> None:
    scope = BuildLogScope("uuid", 7, None, True)

    with pytest.raises(NotConfiguredError):
        await setup.log_service(has_build_log_reader=False).search_build_logs(scope, None, 500)


async def test_search_build_logs_prefers_cloudwatch_over_stored_tail(
    setup: DeploymentSetup,
) -> None:
    setup.build_log_reader.read_events.return_value = BuildLogChunk([], "f/1")
    scope = BuildLogScope("uuid", 7, None, True, [LogLine(1_000, "npm ERR!")], True)

    page = await setup.log_service().search_build_logs(scope, None, 500)

    assert page.entries == []
    assert page.is_partial is False
    setup.build_log_reader.read_events.assert_awaited_once()


@pytest.fixture
async def client(setup: DeploymentSetup) -> AsyncIterator[AsyncClient]:
    user = User(github_id=1001, login="user1")
    user.id = OWNER
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_session] = lambda: AsyncMock()
    app.dependency_overrides[get_deployment_log_service] = lambda: setup.log_service(
        has_build_log_reader=False
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        yield http
    app.dependency_overrides.clear()


async def test_build_logs_api_without_cloudwatch_returns_partial_stored_tail(
    setup: DeploymentSetup, client: AsyncClient
) -> None:
    request_id = await _failed_request_with_tail(setup)

    response = await client.get(
        f"/api/v1/services/{setup.service.id}/deployments/{request_id}/build-logs"
    )

    assert response.status_code == 200
    assert response.json()["data"] == {
        "entries": [
            {
                "timestampNs": str(FAILED_AT_MS * 1_000_000),
                "message": "npm ERR! missing script: build",
            }
        ],
        "buildStatus": "FAILED",
        "isComplete": True,
        "isPartial": True,
        "loggedDeploymentId": request_id,
    }
