from collections.abc import AsyncIterator, Callable
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.clients.aws_clients import BuildLogChunk, LogLine
from app.clients.observability_client import LogEntry, NetworkLogEntry
from app.dependencies import get_current_user, get_deployment_log_service, get_session
from app.enums import DeploymentTrigger
from app.main import app
from app.models.user import User
from app.services.deployment_log_service import DeploymentLogService
from tests.fakes_deployment import OWNER, DeploymentSetup


def _user(id_: int) -> User:
    user = User(github_id=1000 + id_, login=f"user{id_}")
    user.id = id_
    return user


class LogsClient(AsyncClient):
    setup: DeploymentSetup
    session: AsyncMock
    current: dict[str, User]
    log_service: Callable[[], DeploymentLogService]

    def url(self, deployment_id: int, tab: str) -> str:
        return f"/api/v1/services/{self.setup.service.id}/deployments/{deployment_id}/{tab}"


@pytest.fixture
async def client() -> AsyncIterator[LogsClient]:
    setup = await DeploymentSetup().build()
    current = {"user": _user(OWNER)}
    session = AsyncMock()
    app.dependency_overrides[get_current_user] = lambda: current["user"]
    app.dependency_overrides[get_session] = lambda: session
    async with LogsClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        http.setup = setup
        http.session = session
        http.current = current
        http.log_service = setup.log_service
        app.dependency_overrides[get_deployment_log_service] = lambda: http.log_service()
        yield http
    app.dependency_overrides.clear()


async def test_build_logs_returns_entries_cursor_and_releases_session(client: LogsClient) -> None:
    request = await client.setup.create_succeeded_request()
    build = client.setup.builds.builds[0]
    build.codebuild_build_id = "iris-dev-build:uuid-1"
    build.image_repository = "123.dkr.ecr.ap-northeast-2.amazonaws.com/iris/services/1"
    build.succeed("sha256:" + "b" * 64)
    client.setup.build_log_reader.read_events.return_value = BuildLogChunk(
        [LogLine(1_700_000_000_123, "[Container] Running install")], "f/9"
    )

    response = await client.get(client.url(request.id, "build-logs"), params={"limit": 50})

    assert response.status_code == 200
    assert response.json()["data"] == {
        "entries": [
            {"timestampNs": "1700000000123000000", "message": "[Container] Running install"}
        ],
        "nextCursor": "f/9",
        "buildStatus": "SUCCEEDED",
        "isComplete": False,
        "isPartial": False,
        "loggedDeploymentId": request.id,
    }
    client.setup.build_log_reader.read_events.assert_awaited_once_with(
        "/aws/codebuild/x", "uuid-1", 50, None
    )
    client.session.close.assert_awaited_once()


async def test_build_logs_before_codebuild_starts_returns_empty_entries(client: LogsClient) -> None:
    request = await client.setup.manual_service().create_deployment_request(
        OWNER, client.setup.service.id, trigger_type=DeploymentTrigger.MANUAL
    )

    response = await client.get(client.url(request.id, "build-logs"))

    assert response.status_code == 200
    assert response.json()["data"] == {
        "entries": [],
        "buildStatus": "PENDING",
        "isComplete": False,
        "isPartial": False,
    }


async def test_build_logs_without_aws_configuration_returns_503(client: LogsClient) -> None:
    request = await client.setup.create_succeeded_request()
    client.setup.builds.builds[0].codebuild_build_id = "iris-dev-build:uuid-1"
    client.log_service = lambda: client.setup.log_service(has_build_log_reader=False)

    response = await client.get(client.url(request.id, "build-logs"))

    assert response.status_code == 503
    assert response.json()["code"] == "NOT_CONFIGURED"


async def test_deploy_logs_returns_entries_and_the_queried_range(client: LogsClient) -> None:
    request = await client.setup.create_succeeded_request()
    release = client.setup.add_release(request)
    client.setup.observability.search_logs.return_value = [
        LogEntry("1790812800000000000", "listening on 8080", "app-abc", "app")
    ]

    response = await client.get(
        client.url(request.id, "deploy-logs"), params={"limit": 10, "search": "listening"}
    )

    data = response.json()["data"]
    assert response.status_code == 200
    assert data["entries"] == [
        {
            "timestampNs": "1790812800000000000",
            "message": "listening on 8080",
            "pod": "app-abc",
            "container": "app",
        }
    ]
    assert data["isTruncated"] is False
    assert data["start"] < data["end"]
    assert client.setup.observability.search_logs.call_args.args[4:] == (
        10,
        "listening",
        [release.id],
    )
    client.session.close.assert_awaited_once()


async def test_deploy_logs_of_deployment_without_release_returns_empty(client: LogsClient) -> None:
    request = await client.setup.manual_service().create_deployment_request(
        OWNER, client.setup.service.id, trigger_type=DeploymentTrigger.MANUAL
    )

    response = await client.get(client.url(request.id, "deploy-logs"))

    assert response.status_code == 200
    assert response.json()["data"] == {"entries": [], "isTruncated": False}
    client.setup.observability.search_logs.assert_not_awaited()


async def test_network_logs_returns_entries_and_filters_by_status_class(client: LogsClient) -> None:
    request = await client.setup.create_succeeded_request()
    client.setup.add_release(request)
    client.setup.observability.search_network_logs.return_value = [
        NetworkLogEntry("1790812800000000000", 502, None, 120, 0, None),
        NetworkLogEntry("1790812801000000000", 200, 200, 80, 2048, 0.0123),
    ]

    response = await client.get(
        client.url(request.id, "network-logs"), params={"statusClass": "5xx", "limit": 2}
    )

    data = response.json()["data"]
    assert response.status_code == 200
    assert data["entries"] == [
        {"timestampNs": "1790812800000000000", "status": 502, "receivedBytes": 120, "sentBytes": 0},
        {
            "timestampNs": "1790812801000000000",
            "status": 200,
            "targetStatus": 200,
            "receivedBytes": 80,
            "sentBytes": 2048,
            "responseTimeSeconds": 0.0123,
        },
    ]
    assert data["isTruncated"] is True
    assert client.setup.observability.search_network_logs.call_args.args[4:] == (2, "5xx")


async def test_network_logs_of_never_succeeded_deployment_returns_empty(client: LogsClient) -> None:
    request = await client.setup.manual_service().create_deployment_request(
        OWNER, client.setup.service.id, trigger_type=DeploymentTrigger.MANUAL
    )

    response = await client.get(client.url(request.id, "network-logs"))

    assert response.status_code == 200
    assert response.json()["data"] == {"entries": [], "isTruncated": False}


@pytest.mark.parametrize("tab", ["build-logs", "deploy-logs", "network-logs"])
async def test_logs_of_other_users_service_return_not_found(client: LogsClient, tab: str) -> None:
    request = await client.setup.create_succeeded_request()
    client.current["user"] = _user(OWNER + 1)

    response = await client.get(client.url(request.id, tab))

    assert response.status_code == 404
    assert response.json()["code"] == "SERVICE_NOT_FOUND"


@pytest.mark.parametrize("tab", ["build-logs", "deploy-logs", "network-logs"])
async def test_logs_of_unknown_deployment_return_not_found(client: LogsClient, tab: str) -> None:
    response = await client.get(client.url(999, tab))

    assert response.status_code == 404
    assert response.json()["code"] == "DEPLOYMENT_REQUEST_NOT_FOUND"


@pytest.mark.parametrize(
    "tab,params",
    [
        ("build-logs", {"limit": 0}),
        ("build-logs", {"limit": 1001}),
        ("deploy-logs", {"limit": 0}),
        ("deploy-logs", {"targetId": 0}),
        ("network-logs", {"statusClass": "6xx"}),
        ("network-logs", {"limit": 1001}),
    ],
)
async def test_logs_with_invalid_query_return_validation_error(
    client: LogsClient, tab: str, params: dict[str, object]
) -> None:
    response = await client.get(client.url(1, tab), params=params)

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


async def test_deploy_logs_with_target_outside_deployment_returns_invalid_input(
    client: LogsClient,
) -> None:
    request = await client.setup.create_succeeded_request()
    client.setup.add_release(request, target_id=1)

    response = await client.get(client.url(request.id, "deploy-logs"), params={"targetId": 2})

    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_INPUT"
