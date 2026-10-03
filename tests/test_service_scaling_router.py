from collections.abc import AsyncIterator
from copy import deepcopy
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.dependencies import get_current_user, get_service_scaling_service
from app.enums import DeploymentStatus, DeploymentTrigger, JobKind
from app.main import app
from app.models.user import User
from tests.fakes_deployment import OWNER, DeploymentSetup
from tests.test_scaling_config import DEFAULTS
from tests.test_service_scaling_service import SCALED, deployed_request, scaling_service


def _user(id_: int) -> User:
    user = User(github_id=1000 + id_, login=f"user{id_}")
    user.id = id_
    return user


class ScalingClient(AsyncClient):
    setup: DeploymentSetup
    current: dict[str, User]

    @property
    def url(self) -> str:
        return f"/api/v1/services/{self.setup.service.id}/scaling"


@pytest.fixture
async def client() -> AsyncIterator[ScalingClient]:
    setup = await DeploymentSetup().build()
    current = {"user": _user(OWNER)}
    app.dependency_overrides[get_current_user] = lambda: current["user"]
    app.dependency_overrides[get_service_scaling_service] = lambda: scaling_service(setup)
    async with ScalingClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        http.setup = setup
        http.current = current
        yield http
    app.dependency_overrides.clear()


async def test_get_scaling_returns_default_spec_with_camel_case_service_id(
    client: ScalingClient,
) -> None:
    response = await client.get(client.url)

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "data": {"serviceId": client.setup.service.id, **DEFAULTS},
    }


async def test_put_scaling_returns_accepted_deployment_and_persists_spec(
    client: ScalingClient,
) -> None:
    await deployed_request(client.setup)
    jobs_before = len(client.setup.jobs.jobs)

    response = await client.put(client.url, json=SCALED)

    assert response.status_code == 202
    request = client.setup.requests.requests[-1]
    assert response.json() == {
        "success": True,
        "data": {
            "serviceId": client.setup.service.id,
            **SCALED,
            "deploymentRequestId": request.id,
        },
    }
    assert request.trigger_type == DeploymentTrigger.RESTART
    assert [job.kind for job in client.setup.jobs.jobs[jobs_before:]] == [JobKind.DEPLOY]
    saved = await client.get(client.url)
    assert saved.json()["data"]["resources"] == SCALED["resources"]
    assert saved.json()["data"]["replicas"] == 3


async def test_put_scaling_supports_scale_to_zero(client: ScalingClient) -> None:
    await deployed_request(client.setup)

    response = await client.put(client.url, json=DEFAULTS | {"replicas": 0})

    assert response.status_code == 202
    assert response.json()["data"]["replicas"] == 0
    assert client.setup.requests.requests[-1].scaling_snapshot["replicas"] == 0


async def test_put_scaling_idempotency_header_replays_accepted_request(
    client: ScalingClient,
) -> None:
    await deployed_request(client.setup)
    headers = {"Idempotency-Key": "scale-1"}

    first = await client.put(client.url, json=SCALED, headers=headers)
    replayed = await client.put(client.url, json=SCALED, headers=headers)

    assert first.status_code == replayed.status_code == 202
    assert replayed.json() == first.json()
    assert len(client.setup.requests.requests) == 2


async def test_put_scaling_reused_key_with_changed_body_returns_invalid_input(
    client: ScalingClient,
) -> None:
    await deployed_request(client.setup)
    headers = {"Idempotency-Key": "scale-1"}
    await client.put(client.url, json=SCALED, headers=headers)

    response = await client.put(client.url, json=SCALED | {"replicas": 4}, headers=headers)

    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_INPUT"
    assert client.setup.service.scaling_config == SCALED


async def test_put_scaling_during_active_deployment_returns_conflict(
    client: ScalingClient,
) -> None:
    await deployed_request(client.setup)
    await client.setup.manual_service().create_deployment_request(
        OWNER, client.setup.service.id, trigger_type=DeploymentTrigger.RESTART
    )

    response = await client.put(client.url, json=SCALED)

    assert response.status_code == 409
    assert response.json()["code"] == "DEPLOYMENT_IN_PROGRESS"


async def test_put_scaling_before_first_deployment_returns_conflict(
    client: ScalingClient,
) -> None:
    response = await client.put(client.url, json=SCALED)

    assert response.status_code == 409
    assert response.json()["code"] == "NO_SUCCEEDED_DEPLOYMENT"
    assert client.setup.service.scaling_config is None


async def test_put_scaling_after_completed_removal_returns_conflict(
    client: ScalingClient,
) -> None:
    await deployed_request(client.setup)
    removed = await client.setup.manual_service().create_deployment_request(
        OWNER, client.setup.service.id, trigger_type=DeploymentTrigger.REMOVE
    )
    removed.status = DeploymentStatus.SUCCEEDED

    response = await client.put(client.url, json=SCALED)

    assert response.status_code == 409
    assert response.json()["code"] == "NO_SUCCEEDED_DEPLOYMENT"


@pytest.mark.parametrize("method", ["get", "put"])
async def test_scaling_endpoints_hide_other_users_service(
    client: ScalingClient, method: str
) -> None:
    client.current["user"] = _user(OWNER + 1)

    response = (
        await client.put(client.url, json=SCALED)
        if method == "put"
        else await client.get(client.url)
    )

    assert response.status_code == 404
    assert response.json()["code"] == "SERVICE_NOT_FOUND"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"replicas": 3},
        SCALED | {"replicas": -1},
        SCALED | {"replicas": 11},
        SCALED | {"replicas": "3"},
        SCALED | {"replicas": True},
        SCALED | {"podCount": 3},
        SCALED | {"resources": {"limits": SCALED["resources"]["limits"]}},
        SCALED
        | {
            "resources": SCALED["resources"]
            | {"requests": SCALED["resources"]["requests"] | {"cpu": "invalid"}}
        },
        SCALED
        | {
            "resources": SCALED["resources"]
            | {"requests": SCALED["resources"]["requests"] | {"memory": "2Gi"}}
        },
        SCALED
        | {
            "resources": SCALED["resources"]
            | {"limits": SCALED["resources"]["limits"] | {"gpu": "1"}}
        },
    ],
)
async def test_put_scaling_rejects_invalid_body_before_creating_deployment(
    client: ScalingClient, body: dict[str, Any]
) -> None:
    response = await client.put(client.url, json=deepcopy(body))

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert client.setup.requests.requests == []
    assert client.setup.service.scaling_config is None
