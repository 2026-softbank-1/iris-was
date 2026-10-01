from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings, get_settings
from app.dependencies import get_auth_service, get_session_service
from app.main import app
from app.routers.auth_router import OAUTH_NONCE_COOKIE
from app.services.auth_service import AuthService
from app.services.session_service import SessionService
from tests.fakes import (
    FakeGithubInstallationRepository,
    FakeOAuthClient,
    FakeSession,
    FakeUserRepository,
)

SECRET = "test-secret-with-enough-length-for-hs256"


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    settings = Settings(
        database_url="postgresql+asyncpg://t:t@127.0.0.1:1/t",
        session_secret=SECRET,  # type: ignore[arg-type]
        is_session_cookie_secure=False,
        web_base_url="http://web.test",
    )
    users = FakeUserRepository()
    session_service = SessionService(users, SECRET, timedelta(minutes=5))  # type: ignore[arg-type]
    auth_service = AuthService(
        FakeSession(),  # type: ignore[arg-type]
        users,  # type: ignore[arg-type]
        FakeGithubInstallationRepository(),  # type: ignore[arg-type]
        FakeOAuthClient(),
        session_service,
        SECRET,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session_service] = lambda: session_service
    app.dependency_overrides[get_auth_service] = lambda: auth_service
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        yield http
    app.dependency_overrides.clear()


async def _login(client: AsyncClient) -> str:
    start = await client.get("/api/v1/auth/github")
    state = start.headers["location"].split("state=")[1]
    callback = await client.get(
        "/api/v1/auth/github/callback", params={"code": "c", "state": state}
    )
    assert callback.status_code == 302
    return callback.cookies["anydeploy_session"]


async def test_start_login_redirects_to_github_and_sets_nonce_cookie(client: AsyncClient) -> None:
    response = await client.get("/api/v1/auth/github")

    assert response.status_code == 302
    assert response.headers["location"].startswith("https://github.test/authorize?state=")
    assert OAUTH_NONCE_COOKIE in response.cookies
    assert "httponly" in response.headers["set-cookie"].lower()


async def test_callback_sets_session_cookie_and_redirects_to_web(client: AsyncClient) -> None:
    response = await client.get("/api/v1/auth/github")
    state = response.headers["location"].split("state=")[1]

    callback = await client.get(
        "/api/v1/auth/github/callback", params={"code": "c", "state": state}
    )

    assert callback.status_code == 302
    assert callback.headers["location"] == "http://web.test"
    assert "anydeploy_session" in callback.cookies


async def test_callback_with_github_error_redirects_to_login_page(client: AsyncClient) -> None:
    response = await client.get(
        "/api/v1/auth/github/callback", params={"error": "access_denied", "state": "x"}
    )

    assert response.status_code == 302
    assert response.headers["location"] == "http://web.test/login?error=access_denied"


async def test_callback_with_forged_state_returns_unauthorized_envelope(
    client: AsyncClient,
) -> None:
    response = await client.get(
        "/api/v1/auth/github/callback", params={"code": "c", "state": "forged"}
    )

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


async def test_get_me_with_cookie_returns_camel_case_user(client: AsyncClient) -> None:
    token = await _login(client)

    client.cookies.set("anydeploy_session", token)

    response = await client.get("/api/v1/me")

    assert response.status_code == 200
    assert response.json()["data"] == {
        "id": 1,
        "githubId": 1001,
        "login": "octocat",
        "avatarUrl": "http://a/1",
    }


async def test_get_me_with_bearer_token_returns_user(client: AsyncClient) -> None:
    token = await _login(client)
    client.cookies.clear()

    response = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json()["data"]["login"] == "octocat"


async def test_get_me_without_credentials_returns_unauthorized(client: AsyncClient) -> None:
    response = await client.get("/api/v1/me")

    assert response.status_code == 401
    assert response.json() == {
        "success": False,
        "code": "UNAUTHORIZED",
        "message": "login required",
    }


async def test_logout_clears_session_cookie(client: AsyncClient) -> None:
    response = await client.post("/api/v1/auth/logout")

    assert response.status_code == 204
    assert "anydeploy_session" in response.headers["set-cookie"]


async def test_login_without_github_settings_returns_not_configured() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://t:t@127.0.0.1:1/t",
        session_secret=SECRET,  # type: ignore[arg-type]
    )
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
            response = await http.get("/api/v1/auth/github")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json()["code"] == "NOT_CONFIGURED"
