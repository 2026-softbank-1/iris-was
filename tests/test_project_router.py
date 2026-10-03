from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.clients.source_repository_client import BranchInfo
from app.core.exceptions import DeploymentInProgressError
from app.dependencies import (
    get_current_user,
    get_project_service,
    get_service_registry_service,
    get_target_service,
)
from app.main import app
from app.models.user import User
from app.services.project_service import ProjectService
from app.services.service_registry_service import ServiceRegistryService
from app.services.source_repository_service import SourceRepositoryService
from app.services.target_service import TargetService
from tests.fakes import (
    FakeGithubInstallationRepository,
    FakeSession,
    FakeSourceRepositoryClient,
    make_installation,
    make_repository,
)
from tests.fakes_project import (
    FakeProjectRepository,
    FakeServiceRepository,
    FakeTargetRepository,
    FakeTeardownService,
)
from tests.fakes_webhook import FakeDeploymentRequestRepository


def _user(id_: int) -> User:
    user = User(github_id=1000 + id_, login=f"user{id_}")
    user.id = id_
    return user


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    session = FakeSession()
    projects = FakeProjectRepository()
    services = FakeServiceRepository(projects)
    targets = FakeTargetRepository()
    installations = FakeGithubInstallationRepository()
    deployments = FakeDeploymentRequestRepository()
    installation = await installations.save(make_installation(5, 22, "iris-org"))
    await installations.replace_user_links(1, {installation.id})
    github = FakeSourceRepositoryClient({22: [make_repository("iris-org/web")]})
    github.branches["iris-org/web"] = [BranchInfo("main", True)]

    current = {"user": _user(1)}
    app.dependency_overrides[get_current_user] = lambda: current["user"]
    teardown = FakeTeardownService()
    app.dependency_overrides[get_project_service] = lambda: ProjectService(
        session,  # type: ignore[arg-type]
        projects,  # type: ignore[arg-type]
        services,  # type: ignore[arg-type]
        teardown,  # type: ignore[arg-type]
    )
    app.dependency_overrides[get_target_service] = lambda: TargetService(targets)  # type: ignore[arg-type]
    app.dependency_overrides[get_service_registry_service] = lambda: ServiceRegistryService(
        session,  # type: ignore[arg-type]
        projects,  # type: ignore[arg-type]
        services,  # type: ignore[arg-type]
        targets,  # type: ignore[arg-type]
        installations,  # type: ignore[arg-type]
        SourceRepositoryService(installations, github),  # type: ignore[arg-type]
        deployments,  # type: ignore[arg-type]
        teardown,  # type: ignore[arg-type]
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        http.current = current  # type: ignore[attr-defined]
        http.teardown = teardown  # type: ignore[attr-defined]
        yield http
    app.dependency_overrides.clear()


async def _create_project(client: AsyncClient, name: str = "shop") -> int:
    response = await client.post("/api/v1/projects", json={"name": name})
    assert response.status_code == 201
    return int(response.json()["data"]["id"])


async def test_create_project_returns_camel_case_envelope(client: AsyncClient) -> None:
    response = await client.post("/api/v1/projects", json={"name": " shop ", "description": "d"})

    body = response.json()
    assert response.status_code == 201
    assert body["success"] is True
    assert body["data"]["name"] == "shop"
    assert (body["data"]["serviceCount"], body["data"]["onlineServiceCount"]) == (0, 0)
    assert "createdAt" in body["data"]


async def test_create_project_with_blank_name_returns_validation_error(client: AsyncClient) -> None:
    response = await client.post("/api/v1/projects", json={"name": "   "})

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


async def test_create_project_with_duplicate_name_returns_conflict(client: AsyncClient) -> None:
    await _create_project(client)

    response = await client.post("/api/v1/projects", json={"name": "shop"})

    assert response.status_code == 409
    assert response.json()["code"] == "PROJECT_NAME_CONFLICT"


async def test_search_projects_returns_page(client: AsyncClient) -> None:
    for name in ("a", "b", "c"):
        await _create_project(client, name)

    response = await client.get("/api/v1/projects", params={"size": 2})

    data = response.json()["data"]
    assert (data["total"], data["size"], len(data["items"])) == (3, 2, 2)
    assert data["items"][0]["name"] == "c"


async def test_get_project_of_other_user_returns_not_found(client: AsyncClient) -> None:
    project_id = await _create_project(client)
    client.current["user"] = _user(2)  # type: ignore[attr-defined]

    response = await client.get(f"/api/v1/projects/{project_id}")

    assert response.status_code == 404
    assert response.json()["code"] == "PROJECT_NOT_FOUND"


async def test_patch_project_with_null_description_clears_it(client: AsyncClient) -> None:
    created = await client.post("/api/v1/projects", json={"name": "shop", "description": "d"})
    project_id = created.json()["data"]["id"]

    response = await client.patch(f"/api/v1/projects/{project_id}", json={"description": None})

    assert response.status_code == 200
    assert "description" not in response.json()["data"]
    assert response.json()["data"]["name"] == "shop"


async def test_delete_project_returns_no_content_then_not_found(client: AsyncClient) -> None:
    project_id = await _create_project(client)

    deleted = await client.delete(f"/api/v1/projects/{project_id}")
    after = await client.get(f"/api/v1/projects/{project_id}")

    assert deleted.status_code == 204
    assert after.status_code == 404


async def test_service_lifecycle(client: AsyncClient) -> None:
    project_id = await _create_project(client)

    created = await client.post(
        f"/api/v1/projects/{project_id}/services",
        json={"repositoryUrl": "https://github.com/iris-org/web", "targetIds": [1]},
    )
    assert created.status_code == 201
    service = created.json()["data"]
    assert (service["name"], service["sourceBranch"], service["targetIds"]) == ("web", "main", [1])
    assert service["isAutoDeploy"] is True
    assert "builder" not in service

    patched = await client.patch(
        f"/api/v1/services/{service['id']}",
        json={"builder": "railpack", "port": 3000, "isAutoDeploy": False},
    )
    assert patched.json()["data"]["builder"] == "railpack"
    assert patched.json()["data"]["port"] == 3000
    assert patched.json()["data"]["isAutoDeploy"] is False

    listed = await client.get(f"/api/v1/projects/{project_id}/services")
    assert [s["id"] for s in listed.json()["data"]] == [service["id"]]

    assert (await client.delete(f"/api/v1/services/{service['id']}")).status_code == 204
    assert (await client.get(f"/api/v1/services/{service['id']}")).status_code == 404


async def test_create_service_for_inaccessible_repository_returns_forbidden(
    client: AsyncClient,
) -> None:
    project_id = await _create_project(client)

    response = await client.post(
        f"/api/v1/projects/{project_id}/services",
        json={"repositoryUrl": "https://github.com/stranger/repo"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "REPOSITORY_NOT_ACCESSIBLE"


async def test_patch_service_rejects_invalid_port_and_builder(client: AsyncClient) -> None:
    project_id = await _create_project(client)
    created = await client.post(
        f"/api/v1/projects/{project_id}/services",
        json={"repositoryUrl": "iris-org/web"},
    )
    service_id = created.json()["data"]["id"]

    port = await client.patch(f"/api/v1/services/{service_id}", json={"port": 70000})
    builder = await client.patch(f"/api/v1/services/{service_id}", json={"builder": "nixpacks"})

    assert port.status_code == builder.status_code == 422


async def test_search_targets_lists_seeded_targets(client: AsyncClient) -> None:
    response = await client.get("/api/v1/targets")

    assert [(t["name"], t["kind"]) for t in response.json()["data"]] == [
        ("aws", "AWS"),
        ("onprem", "ONPREM"),
    ]


async def test_service_response_has_no_latest_deployment_before_first_deploy(
    client: AsyncClient,
) -> None:
    project_id = await _create_project(client)
    created = await client.post(
        f"/api/v1/projects/{project_id}/services",
        json={"repositoryUrl": "https://github.com/iris-org/web"},
    )

    assert created.status_code == 201
    assert "latestDeployment" not in created.json()["data"]


async def test_delete_service_with_deployment_in_progress_returns_conflict(
    client: AsyncClient,
) -> None:
    project_id = (await client.post("/api/v1/projects", json={"name": "shop"})).json()["data"]["id"]
    service = (
        await client.post(
            f"/api/v1/projects/{project_id}/services",
            json={"repositoryUrl": "https://github.com/iris-org/web"},
        )
    ).json()["data"]
    client.teardown.error = DeploymentInProgressError(  # type: ignore[attr-defined]
        "a deployment is in progress", service_id=service["id"]
    )

    response = await client.delete(f"/api/v1/services/{service['id']}")

    assert response.status_code == 409
    assert response.json()["code"] == "DEPLOYMENT_IN_PROGRESS"
    # 지워지지 않았으니 그대로 조회된다.
    assert (await client.get(f"/api/v1/services/{service['id']}")).status_code == 200


async def test_delete_project_with_deployment_in_progress_returns_conflict(
    client: AsyncClient,
) -> None:
    project_id = (await client.post("/api/v1/projects", json={"name": "shop"})).json()["data"]["id"]
    client.teardown.error = DeploymentInProgressError(  # type: ignore[attr-defined]
        "a deployment is in progress", service_id=1
    )

    response = await client.delete(f"/api/v1/projects/{project_id}")

    assert response.status_code == 409
    assert response.json()["code"] == "DEPLOYMENT_IN_PROGRESS"
    assert (await client.get(f"/api/v1/projects/{project_id}")).status_code == 200
