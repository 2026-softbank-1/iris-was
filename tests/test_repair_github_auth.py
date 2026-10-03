from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.exceptions import ConflictError, ServiceNotFoundError, UnauthorizedError
from app.dependencies import get_current_user, get_repair_github_auth_service
from app.main import app
from app.models.user import User
from app.services.repair_github_auth_service import RepairGithubAuthService
from app.services.source_repository_service import SourceRepositoryService
from tests.fakes import (
    FakeGithubInstallationRepository,
    FakeSourceRepositoryClient,
    make_installation,
    make_repository,
)
from tests.fakes_diagnosis import OWNER, DiagnosisSetup


@pytest.fixture
async def auth_setup() -> tuple[
    DiagnosisSetup, RepairGithubAuthService, FakeSourceRepositoryClient
]:
    setup = await DiagnosisSetup().build()
    setup.service.source_repository_url = "https://github.com/iris-org/web"
    installations = FakeGithubInstallationRepository()
    await installations.save(make_installation(1, 22, "iris-org"))
    await installations.replace_user_links(OWNER, {1})
    github = FakeSourceRepositoryClient({22: [make_repository("iris-org/web")]})
    repositories = SourceRepositoryService(installations, github)  # type: ignore[arg-type]
    service = RepairGithubAuthService(setup.services, repositories)  # type: ignore[arg-type]
    return setup, service, github


async def test_issue_token_requires_service_owner_and_exact_source(auth_setup: tuple) -> None:
    setup, service, github = auth_setup
    with pytest.raises(ServiceNotFoundError):
        await service.issue_token(OWNER + 1, setup.service.id, "iris-org/web")
    with pytest.raises(ConflictError):
        await service.issue_token(OWNER, setup.service.id, "iris-org/other")
    assert github.repair_token_requests == []
    result = await service.issue_token(OWNER, setup.service.id, "IRIS-ORG/Web")
    assert result.repository == "iris-org/web"
    assert "ghs_repair_fake" not in repr(result)
    assert github.repair_token_requests == [(22, "iris-org/web")]


@pytest.fixture
async def client(auth_setup: tuple) -> AsyncIterator[AsyncClient]:
    _, service, _ = auth_setup
    user = User(github_id=1000 + OWNER, login=f"user{OWNER}")
    user.id = OWNER
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_repair_github_auth_service] = lambda: service
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
            yield http
    finally:
        app.dependency_overrides.clear()


async def test_token_endpoint_returns_uncacheable_scoped_credentials(
    client: AsyncClient, auth_setup: tuple
) -> None:
    setup, _, github = auth_setup
    response = await client.post(
        f"/api/v1/services/{setup.service.id}/repair-github-token",
        json={"repository": "iris-org/web"},
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.json()["data"] == {
        "repository": "iris-org/web",
        "token": "ghs_repair_fake",
        "expiresAt": "2030-01-01T00:00:00Z",
    }
    assert github.repair_token_requests == [(22, "iris-org/web")]


async def test_token_endpoint_requires_login_and_hides_other_users_service(
    client: AsyncClient, auth_setup: tuple
) -> None:
    setup, _, github = auth_setup
    url = f"/api/v1/services/{setup.service.id}/repair-github-token"
    other = User(github_id=2000, login="other")
    other.id = OWNER + 1
    app.dependency_overrides[get_current_user] = lambda: other
    assert (await client.post(url, json={"repository": "iris-org/web"})).status_code == 404

    def no_login() -> User:
        raise UnauthorizedError("login required")

    app.dependency_overrides[get_current_user] = no_login
    assert (await client.post(url, json={"repository": "iris-org/web"})).status_code == 401
    assert github.repair_token_requests == []


async def test_token_endpoint_rejects_repo_override_and_invalid_body(
    client: AsyncClient, auth_setup: tuple
) -> None:
    setup, _, github = auth_setup
    url = f"/api/v1/services/{setup.service.id}/repair-github-token"
    assert (await client.post(url, json={"repository": "iris-org/other"})).status_code == 409
    assert (await client.post(url, json={"repository": "../web/extra"})).status_code == 422
    assert github.repair_token_requests == []


async def test_token_endpoint_authenticates_existing_was_session_jwt(
    client: AsyncClient, auth_setup: tuple
) -> None:
    from datetime import timedelta

    from app.dependencies import get_session_service
    from app.services.session_service import SessionService
    from tests.fakes import FakeUserRepository

    setup, _, github = auth_setup
    user = User(github_id=1000 + OWNER, login=f"user{OWNER}")
    user.id = OWNER
    sessions = SessionService(
        FakeUserRepository([user]), "offline-test-session-secret-32-characters", timedelta(hours=1)
    )  # type: ignore[arg-type]
    app.dependency_overrides.pop(get_current_user)
    app.dependency_overrides[get_session_service] = lambda: sessions
    url = f"/api/v1/services/{setup.service.id}/repair-github-token"
    for token in (None, "invalid-session"):
        headers = {} if token is None else {"Authorization": f"Bearer {token}"}
        response = await client.post(url, json={"repository": "iris-org/web"}, headers=headers)
        assert response.status_code == 401
    assert github.repair_token_requests == []
    response = await client.post(
        url,
        json={"repository": "iris-org/web"},
        headers={"Authorization": f"Bearer {sessions.create_session_token(user)}"},
    )
    assert response.status_code == 200
    assert github.repair_token_requests == [(22, "iris-org/web")]
