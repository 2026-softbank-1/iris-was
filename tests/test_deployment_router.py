from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.dependencies import (
    get_current_user,
    get_deployment_history_service,
    get_manual_deployment_service,
)
from app.enums import DeploymentStatus
from app.main import app
from app.models.user import User
from tests.fakes_deployment import HEAD_SHA, OWNER, DeploymentSetup


def _user(id_: int) -> User:
    user = User(github_id=1000 + id_, login=f"user{id_}")
    user.id = id_
    return user


class DeploymentClient(AsyncClient):
    setup: DeploymentSetup
    current: dict[str, User]

    @property
    def url(self) -> str:
        return f"/api/v1/services/{self.setup.service.id}/deployments"


@pytest.fixture
async def client() -> AsyncIterator[DeploymentClient]:
    setup = await DeploymentSetup().build()
    current = {"user": _user(OWNER)}
    app.dependency_overrides[get_current_user] = lambda: current["user"]
    app.dependency_overrides[get_manual_deployment_service] = setup.manual_service
    app.dependency_overrides[get_deployment_history_service] = setup.history_service
    async with DeploymentClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        http.setup = setup
        http.current = current
        yield http
    app.dependency_overrides.clear()


async def test_create_deployment_request_returns_created_camel_case_envelope(
    client: DeploymentClient,
) -> None:
    response = await client.post(client.url, json={"triggerType": "MANUAL"})

    body = response.json()
    assert response.status_code == 201
    assert body["success"] is True
    assert body["data"]["status"] == "QUEUED"
    assert body["data"]["triggerType"] == "MANUAL"
    assert body["data"]["sourceSha"] == HEAD_SHA
    assert body["data"]["sourceCommitMessage"] == "feat: add login"
    assert body["data"]["isActive"] is True
    assert body["data"]["requestedBy"] == OWNER
    assert "failureCode" not in body["data"]


async def test_create_deployment_request_with_idempotency_key_replays_first_request(
    client: DeploymentClient,
) -> None:
    headers = {"Idempotency-Key": "click-1"}

    first = await client.post(client.url, json={"triggerType": "MANUAL"}, headers=headers)
    second = await client.post(client.url, json={"triggerType": "MANUAL"}, headers=headers)

    assert second.status_code == 201
    assert second.json()["data"]["id"] == first.json()["data"]["id"]


async def test_create_deployment_request_while_active_returns_conflict(
    client: DeploymentClient,
) -> None:
    await client.post(client.url, json={"triggerType": "MANUAL"})

    response = await client.post(client.url, json={"triggerType": "MANUAL"})

    assert response.status_code == 409
    assert response.json()["code"] == "DEPLOYMENT_IN_PROGRESS"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"triggerType": "PUSH"},
        {"triggerType": "MANUAL", "sourceDeploymentId": 1},
        {"triggerType": "REDEPLOY"},
        {"triggerType": "ROLLBACK", "sourceDeploymentId": 1, "sourceSha": "abcdef1"},
        {"triggerType": "MANUAL", "sourceSha": "not-hex"},
        {"triggerType": "RESTART", "sourceDeploymentId": 1},
        {"triggerType": "RESTART", "sourceSha": "abcdef1"},
    ],
)
async def test_create_deployment_request_with_invalid_body_returns_validation_error(
    client: DeploymentClient, body: dict[str, object]
) -> None:
    response = await client.post(client.url, json=body)

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


async def test_create_deployment_request_restart_returns_deploying_with_source(
    client: DeploymentClient,
) -> None:
    first = await client.post(client.url, json={"triggerType": "MANUAL"})
    source_id = first.json()["data"]["id"]
    build = client.setup.builds.builds[0]
    build.image_repository = "123.dkr.ecr.ap-northeast-2.amazonaws.com/iris/services/1"
    build.succeed("sha256:" + "b" * 64)
    client.setup.requests.requests[0].status = DeploymentStatus.SUCCEEDED

    response = await client.post(client.url, json={"triggerType": "RESTART"})

    body = response.json()
    assert response.status_code == 201
    assert body["data"]["triggerType"] == "RESTART"
    assert body["data"]["status"] == "DEPLOYING"
    assert body["data"]["sourceDeploymentId"] == source_id
    assert body["data"]["isActive"] is True


async def test_create_deployment_request_restart_without_succeeded_deployment_returns_conflict(
    client: DeploymentClient,
) -> None:
    response = await client.post(client.url, json={"triggerType": "RESTART"})

    assert response.status_code == 409
    assert response.json()["code"] == "NO_SUCCEEDED_DEPLOYMENT"


async def test_create_deployment_request_of_other_users_service_returns_not_found(
    client: DeploymentClient,
) -> None:
    client.current["user"] = _user(OWNER + 1)

    response = await client.post(client.url, json={"triggerType": "MANUAL"})

    assert response.status_code == 404
    assert response.json()["code"] == "SERVICE_NOT_FOUND"


async def test_create_deployment_request_redeploy_of_unknown_deployment_returns_not_found(
    client: DeploymentClient,
) -> None:
    response = await client.post(
        client.url, json={"triggerType": "REDEPLOY", "sourceDeploymentId": 999}
    )

    assert response.status_code == 404
    assert response.json()["code"] == "DEPLOYMENT_REQUEST_NOT_FOUND"


async def test_search_deployment_requests_returns_newest_first_page(
    client: DeploymentClient,
) -> None:
    for _ in range(3):
        await client.post(client.url, json={"triggerType": "MANUAL"})
        client.setup.finish_active_requests()

    response = await client.get(client.url, params={"size": 2})

    data = response.json()["data"]
    assert response.status_code == 200
    assert [item["id"] for item in data["items"]] == [3, 2]
    assert (data["total"], data["page"], data["size"]) == (3, 0, 2)


async def test_search_deployment_requests_with_oversized_page_size_returns_validation_error(
    client: DeploymentClient,
) -> None:
    response = await client.get(client.url, params={"size": 101})

    assert response.status_code == 422


async def test_get_deployment_request_returns_history_and_stages(
    client: DeploymentClient,
) -> None:
    created = await client.post(client.url, json={"triggerType": "MANUAL"})
    deployment_id = created.json()["data"]["id"]
    status_service = client.setup.status_service()
    await status_service.transition_status(deployment_id, DeploymentStatus.BUILDING)
    await status_service.transition_status(deployment_id, DeploymentStatus.DEPLOYING)
    await status_service.transition_status(deployment_id, DeploymentStatus.SUCCEEDED)

    response = await client.get(f"{client.url}/{deployment_id}")

    data = response.json()["data"]
    assert response.status_code == 200
    assert data["status"] == "SUCCEEDED"
    assert data["isActive"] is False
    assert [h["toStatus"] for h in data["history"]] == [
        "QUEUED",
        "BUILDING",
        "DEPLOYING",
        "SUCCEEDED",
    ]
    assert "fromStatus" not in data["history"][0]
    assert [s["status"] for s in data["stages"]] == ["QUEUED", "BUILDING", "DEPLOYING", "SUCCEEDED"]
    assert all("durationSeconds" in s for s in data["stages"][:-1])
    assert "durationSeconds" not in data["stages"][-1]


async def test_get_deployment_request_of_other_users_service_returns_not_found(
    client: DeploymentClient,
) -> None:
    created = await client.post(client.url, json={"triggerType": "MANUAL"})
    client.current["user"] = _user(OWNER + 1)

    response = await client.get(f"{client.url}/{created.json()['data']['id']}")

    assert response.status_code == 404
    assert response.json()["code"] == "SERVICE_NOT_FOUND"
