from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.dependencies import get_current_user, get_repair_service, get_repair_service_opener
from app.main import app
from app.models.user import User
from tests.fakes_repair import OWNER, RepairSetup


@pytest.fixture
async def client() -> AsyncIterator[tuple[AsyncClient, RepairSetup, dict[str, int]]]:
    setup = RepairSetup()
    await setup.build()
    owner = {"id": OWNER}

    def current_user() -> User:
        return User(id=owner["id"], github_id=1000 + owner["id"], login="repair-test")

    app.dependency_overrides[get_current_user] = current_user
    app.dependency_overrides[get_repair_service] = setup.repair_service
    app.dependency_overrides[get_repair_service_opener] = setup.repair_service_opener
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http, setup, owner
    app.dependency_overrides.clear()


async def test_repair_post_202_and_poll_candidate_without_deployment_change(
    client: tuple[AsyncClient, RepairSetup, dict[str, int]],
) -> None:
    http, setup, _ = client
    deployment, diagnosis = setup.add_repair_inputs()
    setup.repair_agent.candidate = True
    started = await http.post(
        f"/api/v1/services/{setup.service.id}/deployments/{deployment}/repairs",
        headers={"Idempotency-Key": "test-key"},
        json={"diagnosisId": diagnosis, "planIds": ["R1"]},
    )
    assert started.status_code == 202
    assert started.json()["data"]["status"] == "RUNNING"
    repair_id = started.json()["data"]["id"]
    result = await http.get(f"/api/v1/services/{setup.service.id}/repairs/{repair_id}")
    assert result.status_code == 200
    data = result.json()["data"]
    assert data["status"] == "SUCCEEDED"
    assert data["result"]["status"] == "candidate_ready"
    assert data["result"]["validation"]["status"] == "not_run"
    assert data["result"]["deploymentAuthorized"] is False
    patch = await http.get(
        f"/api/v1/services/{setup.service.id}/repairs/{repair_id}/artifacts/patch.diff"
    )
    assert patch.status_code == 200 and patch.content == b"patch.diff"
    assert all("/api/v1/services/" in item["url"] for item in data["result"]["artifacts"])


async def test_repair_idempotency_returns_existing_and_conflict(
    client: tuple[AsyncClient, RepairSetup, dict[str, int]],
) -> None:
    http, setup, _ = client
    deployment, diagnosis = setup.add_repair_inputs()
    url = f"/api/v1/services/{setup.service.id}/deployments/{deployment}/repairs"
    headers = {"Idempotency-Key": "key"}
    body = {"diagnosisId": diagnosis, "planIds": ["R1"]}
    first = await http.post(url, headers=headers, json=body)
    second = await http.post(url, headers=headers, json=body)
    assert first.status_code == 202 and second.status_code == 200
    assert first.json()["data"]["id"] == second.json()["data"]["id"]
    deployment2, diagnosis2 = setup.add_repair_inputs()
    conflict = await http.post(
        f"/api/v1/services/{setup.service.id}/deployments/{deployment2}/repairs",
        headers=headers,
        json={"diagnosisId": diagnosis2, "planIds": ["R1"]},
    )
    assert conflict.status_code == 409
    assert len(setup.repair_agent.requests) == 1


async def test_repair_routes_enforce_owner_and_reject_client_source_policy(
    client: tuple[AsyncClient, RepairSetup, dict[str, int]],
) -> None:
    http, setup, owner = client
    deployment, diagnosis = setup.add_repair_inputs()
    url = f"/api/v1/services/{setup.service.id}/deployments/{deployment}/repairs"
    body = {"diagnosisId": diagnosis, "planIds": ["R1"]}
    owner["id"] = OWNER + 1
    unauthorized = await http.post(url, headers={"Idempotency-Key": "key"}, json=body)
    assert unauthorized.status_code == 404
    owner["id"] = OWNER
    invalid = await http.post(
        url,
        headers={"Idempotency-Key": "key"},
        json={**body, "source": {"downloadUrl": "https://untrusted"}},
    )
    assert invalid.status_code == 422
    missing_key = await http.post(url, json=body)
    assert missing_key.status_code == 422
    assert setup.repairs.rows == []


async def test_repair_artifact_corruption_and_ownership_blocked(
    client: tuple[AsyncClient, RepairSetup, dict[str, int]],
) -> None:
    http, setup, owner = client
    deployment, diagnosis = setup.add_repair_inputs()
    setup.repair_agent.candidate = True
    started = await http.post(
        f"/api/v1/services/{setup.service.id}/deployments/{deployment}/repairs",
        headers={"Idempotency-Key": "key"},
        json={"diagnosisId": diagnosis, "planIds": ["R1"]},
    )
    repair_id = started.json()["data"]["id"]
    url = f"/api/v1/services/{setup.service.id}/repairs/{repair_id}/artifacts/patch.diff"
    owner["id"] = OWNER + 1
    assert (await http.get(url)).status_code == 404
    owner["id"] = OWNER
    setup.repair_agent.artifacts["patch.diff"] = b"corrupt"
    assert (await http.get(url)).status_code == 502
