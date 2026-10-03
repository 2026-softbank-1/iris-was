from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from app.clients.source_repository_client import BranchInfo
from app.core.config import Settings, get_settings
from app.dependencies import (
    get_auth_service,
    get_current_user,
    get_source_repository_service,
)
from app.main import app
from app.models.user import User
from app.services.auth_service import AuthService
from app.services.cli_login_service import CliLoginService
from app.services.session_service import SessionService
from app.services.source_repository_service import SourceRepositoryService
from tests.fakes import (
    FakeCliLoginSessionRepository,
    FakeGithubInstallationRepository,
    FakeOAuthClient,
    FakeSession,
    FakeSourceRepositoryClient,
    FakeUserRepository,
    make_installation,
    make_repository,
)

SECRET = "test-secret-with-enough-length-for-hs256"


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    settings = Settings(
        database_url="postgresql+asyncpg://t:t@127.0.0.1:1/t",
        session_secret=SECRET,  # type: ignore[arg-type]
        is_session_cookie_secure=False,
        github_app_slug="anydeploy-test",
    )
    installations = FakeGithubInstallationRepository()
    await installations.save(make_installation(1, 22, "iris-org"))
    await installations.replace_user_links(1, {1})
    source_client = FakeSourceRepositoryClient(
        {22: [make_repository(f"iris-org/repo{n:02d}") for n in range(25)]}
    )
    source_client.branches["iris-org/repo00"] = [BranchInfo("main", True), BranchInfo("dev", False)]
    user = User(github_id=1001, login="octocat")
    user.id = 1
    users = FakeUserRepository([user])
    db_session = FakeSession()
    session_service = SessionService(users, SECRET, timedelta(minutes=5))  # type: ignore[arg-type]
    auth_service = AuthService(
        db_session,  # type: ignore[arg-type]
        users,  # type: ignore[arg-type]
        installations,  # type: ignore[arg-type]
        FakeOAuthClient(),
        session_service,
        CliLoginService(
            db_session,  # type: ignore[arg-type]
            FakeCliLoginSessionRepository(),  # type: ignore[arg-type]
            users,  # type: ignore[arg-type]
            session_service,
        ),
        SECRET,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_auth_service] = lambda: auth_service
    app.dependency_overrides[get_source_repository_service] = lambda: SourceRepositoryService(
        installations,  # type: ignore[arg-type]
        source_client,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        yield http
    app.dependency_overrides.clear()


async def test_install_redirects_to_github_app_page_with_state_cookie(client: AsyncClient) -> None:
    response = await client.get("/api/v1/github/install")

    assert response.status_code == 302
    assert response.headers["location"].startswith(
        "https://github.test/apps/anydeploy-test/installations/new?state="
    )
    assert "anydeploy_oauth_nonce" in response.cookies


async def test_search_installations_returns_camel_case_items(client: AsyncClient) -> None:
    response = await client.get("/api/v1/github/installations")

    assert response.json()["data"] == [
        {"installationId": 22, "accountLogin": "iris-org", "accountType": "Organization"}
    ]


async def test_search_repositories_returns_page_envelope(client: AsyncClient) -> None:
    response = await client.get("/api/v1/github/repos", params={"size": 10, "page": 2})

    data = response.json()["data"]
    assert (data["total"], data["page"], data["size"]) == (25, 2, 10)
    assert [r["fullName"] for r in data["items"]][0] == "iris-org/repo20"
    assert data["items"][0]["installationId"] == 22


async def test_search_repositories_filters_by_query(client: AsyncClient) -> None:
    response = await client.get("/api/v1/github/repos", params={"q": "repo07"})

    assert [r["fullName"] for r in response.json()["data"]["items"]] == ["iris-org/repo07"]


async def test_search_repositories_rejects_oversized_page_size(client: AsyncClient) -> None:
    response = await client.get("/api/v1/github/repos", params={"size": 1000})

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


async def test_resolve_repository_accepts_pasted_url(client: AsyncClient) -> None:
    response = await client.get(
        "/api/v1/github/repos/resolve", params={"url": "https://github.com/iris-org/repo03.git"}
    )

    assert response.status_code == 200
    assert response.json()["data"]["fullName"] == "iris-org/repo03"


async def test_resolve_repository_without_permission_returns_forbidden(client: AsyncClient) -> None:
    response = await client.get(
        "/api/v1/github/repos/resolve", params={"url": "https://github.com/stranger/repo"}
    )

    assert response.status_code == 403
    assert response.json()["code"] == "REPOSITORY_NOT_ACCESSIBLE"


async def test_resolve_repository_with_invalid_url_returns_invalid_input(
    client: AsyncClient,
) -> None:
    response = await client.get("/api/v1/github/repos/resolve", params={"url": "nonsense"})

    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_INPUT"


async def test_search_branches_returns_default_flag(client: AsyncClient) -> None:
    response = await client.get("/api/v1/github/repos/iris-org/repo00/branches")

    assert response.json()["data"] == [
        {"name": "main", "isDefault": True},
        {"name": "dev", "isDefault": False},
    ]
