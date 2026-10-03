from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.clients.source_repository_client import BranchInfo
from app.dependencies import (
    get_current_user,
    get_domain_service,
    get_project_service,
    get_service_registry_service,
)
from app.main import app
from app.models.user import User
from app.services.domain_service import DomainService
from app.services.project_service import ProjectService
from app.services.service_registry_service import ServiceRegistryService
from app.services.source_repository_service import SourceRepositoryService
from tests.fakes import (
    FakeGithubInstallationRepository,
    FakeSession,
    FakeSourceRepositoryClient,
    make_installation,
    make_repository,
)
from tests.fakes_domain import FakeReleaseRepository
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
    targets.targets[0].domain_suffix = "likelion.uk"
    releases = FakeReleaseRepository()
    installations = FakeGithubInstallationRepository()
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
    app.dependency_overrides[get_service_registry_service] = lambda: ServiceRegistryService(
        session,  # type: ignore[arg-type]
        projects,  # type: ignore[arg-type]
        services,  # type: ignore[arg-type]
        targets,  # type: ignore[arg-type]
        installations,  # type: ignore[arg-type]
        SourceRepositoryService(installations, github),  # type: ignore[arg-type]
        FakeDeploymentRequestRepository(),  # type: ignore[arg-type]
        teardown,  # type: ignore[arg-type]
    )
    app.dependency_overrides[get_domain_service] = lambda: DomainService(
        services,  # type: ignore[arg-type]
        targets,  # type: ignore[arg-type]
        releases,  # type: ignore[arg-type]
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        http.current = current  # type: ignore[attr-defined]
        http.releases = releases  # type: ignore[attr-defined]
        yield http
    app.dependency_overrides.clear()


async def _create_service(client: AsyncClient, target_ids: list[int] | None = None) -> int:
    project = await client.post("/api/v1/projects", json={"name": "shop"})
    project_id = project.json()["data"]["id"]
    body: dict[str, object] = {"repositoryUrl": "https://github.com/iris-org/web"}
    if target_ids is not None:
        body["targetIds"] = target_ids
    created = await client.post(f"/api/v1/projects/{project_id}/services", json=body)
    assert created.status_code == 201
    return int(created.json()["data"]["id"])


async def test_search_domains_returns_host_and_url_per_target(client: AsyncClient) -> None:
    service_id = await _create_service(client)

    response = await client.get(f"/api/v1/services/{service_id}/domains")

    assert response.status_code == 200
    aws, local = response.json()["data"]
    assert aws == {
        "targetId": 1,
        "targetName": "aws",
        "targetKind": "AWS",
        "host": f"web-{service_id}.likelion.uk",
        "url": f"https://web-{service_id}.likelion.uk",
        "isConnected": False,
    }
    assert local == {
        "targetId": 2,
        "targetName": "local",
        "targetKind": "LOCAL",
        "isConnected": False,
    }


async def test_search_domains_is_connected_after_successful_release(client: AsyncClient) -> None:
    service_id = await _create_service(client)
    client.releases.connected.add((service_id, 1))  # type: ignore[attr-defined]

    response = await client.get(f"/api/v1/services/{service_id}/domains")

    assert [d["isConnected"] for d in response.json()["data"]] == [True, False]


async def test_search_domains_only_lists_linked_targets(client: AsyncClient) -> None:
    service_id = await _create_service(client, target_ids=[1])

    response = await client.get(f"/api/v1/services/{service_id}/domains")

    assert [d["targetName"] for d in response.json()["data"]] == ["aws"]


async def test_search_domains_of_other_user_returns_not_found(client: AsyncClient) -> None:
    service_id = await _create_service(client)
    client.current["user"] = _user(2)  # type: ignore[attr-defined]

    response = await client.get(f"/api/v1/services/{service_id}/domains")

    assert response.status_code == 404
    assert response.json()["code"] == "SERVICE_NOT_FOUND"
