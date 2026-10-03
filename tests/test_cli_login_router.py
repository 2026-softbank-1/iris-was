from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings, get_settings
from app.dependencies import get_auth_service, get_cli_login_service, get_session_service
from app.main import app
from app.services.auth_service import AuthService
from app.services.cli_login_service import CliLoginService
from app.services.session_service import SessionService
from tests.fakes import (
    FakeCliLoginSessionRepository,
    FakeGithubInstallationRepository,
    FakeOAuthClient,
    FakeSession,
    FakeUserRepository,
)

SECRET = "test-secret-with-enough-length-for-hs256"
BASE = "/api/v1/auth/cli/sessions"


@dataclass
class Env:
    client: AsyncClient
    cli_sessions: FakeCliLoginSessionRepository
    settings: Settings


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://t:t@127.0.0.1:1/t",
        session_secret=SECRET,  # type: ignore[arg-type]
        is_session_cookie_secure=False,
        web_base_url="http://web.test",
        api_base_url="https://api.test",
    )


@pytest.fixture
async def env() -> AsyncIterator[Env]:
    settings = _settings()
    users = FakeUserRepository()
    cli_sessions = FakeCliLoginSessionRepository()
    db_session = FakeSession()
    session_service = SessionService(users, SECRET, timedelta(minutes=5))  # type: ignore[arg-type]
    cli_login_service = CliLoginService(
        db_session,  # type: ignore[arg-type]
        cli_sessions,  # type: ignore[arg-type]
        users,  # type: ignore[arg-type]
        session_service,
    )
    auth_service = AuthService(
        db_session,  # type: ignore[arg-type]
        users,  # type: ignore[arg-type]
        FakeGithubInstallationRepository(),  # type: ignore[arg-type]
        FakeOAuthClient(),
        session_service,
        cli_login_service,
        SECRET,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session_service] = lambda: session_service
    app.dependency_overrides[get_cli_login_service] = lambda: cli_login_service
    app.dependency_overrides[get_auth_service] = lambda: auth_service
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        yield Env(http, cli_sessions, settings)
    app.dependency_overrides.clear()


async def _create(client: AsyncClient) -> dict[str, object]:
    response = await client.post(BASE)
    assert response.status_code == 201
    data: dict[str, object] = response.json()["data"]
    return data


async def _approve_in_browser(client: AsyncClient, session_id: str) -> None:
    authorize = await client.get(f"{BASE}/{session_id}/authorize")
    state = authorize.headers["location"].split("state=")[1]
    callback = await client.get(
        "/api/v1/auth/github/callback", params={"code": "c", "state": state}
    )
    assert callback.status_code == 200


async def test_create_session_returns_camel_case_contract(env: Env) -> None:
    response = await env.client.post(BASE)

    assert response.status_code == 201
    data = response.json()["data"]
    assert set(data) == {"sessionId", "pollSecret", "verificationUrl", "expiresIn", "interval"}
    assert data["verificationUrl"] == f"https://api.test{BASE}/{data['sessionId']}/authorize"
    assert data["expiresIn"] == 600
    assert data["interval"] == 2


async def test_create_session_without_api_base_url_uses_request_host(env: Env) -> None:
    settings = _settings()
    settings.api_base_url = None
    app.dependency_overrides[get_settings] = lambda: settings

    data = await _create(env.client)

    assert data["verificationUrl"] == f"http://t{BASE}/{data['sessionId']}/authorize"


async def test_create_session_without_session_secret_returns_not_configured() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]  # 로컬 .env 의 설정이 섞이지 않게 한다
        database_url="postgresql+asyncpg://t:t@127.0.0.1:1/t",
        session_secret=None,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
            response = await http.post(BASE)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json()["code"] == "NOT_CONFIGURED"


async def test_authorize_redirects_to_github_with_nonce_cookie(env: Env) -> None:
    data = await _create(env.client)

    response = await env.client.get(f"{BASE}/{data['sessionId']}/authorize")

    assert response.status_code == 302
    assert response.headers["location"].startswith("https://github.test/authorize?state=")
    assert "anydeploy_oauth_nonce" in response.cookies
    assert "httponly" in response.headers["set-cookie"].lower()


async def test_authorize_unknown_session_returns_not_found_envelope(env: Env) -> None:
    response = await env.client.get(f"{BASE}/unknown/authorize")

    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


async def test_authorize_expired_session_returns_not_found(env: Env) -> None:
    data = await _create(env.client)
    stored = await env.cli_sessions.find_by_public_id(str(data["sessionId"]))
    assert stored is not None
    stored.expires_at = datetime.now(UTC) - timedelta(seconds=1)

    response = await env.client.get(f"{BASE}/{data['sessionId']}/authorize")

    assert response.status_code == 404


async def test_cli_login_flow_issues_token_once_and_me_shows_github_account(env: Env) -> None:
    data = await _create(env.client)
    session_id, poll_secret = data["sessionId"], data["pollSecret"]
    poll_url = f"{BASE}/{session_id}/token"

    pending = await env.client.post(poll_url, json={"pollSecret": poll_secret})
    await _approve_in_browser(env.client, str(session_id))
    approved = await env.client.post(poll_url, json={"pollSecret": poll_secret})
    consumed = await env.client.post(poll_url, json={"pollSecret": poll_secret})

    assert pending.status_code == 200
    assert pending.json()["data"] == {"status": "PENDING"}
    assert approved.json()["data"]["status"] == "APPROVED"
    token = approved.json()["data"]["accessToken"]
    assert consumed.json()["data"] == {"status": "EXPIRED"}
    env.client.cookies.clear()
    me = await env.client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["data"]["login"] == "octocat"


async def test_cli_callback_shows_return_to_terminal_page_without_web_session_cookie(
    env: Env,
) -> None:
    data = await _create(env.client)
    authorize = await env.client.get(f"{BASE}/{data['sessionId']}/authorize")
    state = authorize.headers["location"].split("state=")[1]

    callback = await env.client.get(
        "/api/v1/auth/github/callback", params={"code": "c", "state": state}
    )

    assert callback.status_code == 200
    assert callback.headers["content-type"].startswith("text/html")
    assert "터미널로 돌아가세요" in callback.text
    assert callback.headers["cache-control"] == "no-store"
    set_cookies = " ".join(callback.headers.get_list("set-cookie"))
    assert "anydeploy_session" not in set_cookies
    assert "anydeploy_oauth_nonce" in set_cookies  # 쓴 nonce 쿠키는 지운다


async def test_cli_callback_from_another_browser_without_nonce_is_unauthorized(env: Env) -> None:
    data = await _create(env.client)
    authorize = await env.client.get(f"{BASE}/{data['sessionId']}/authorize")
    state = authorize.headers["location"].split("state=")[1]
    env.client.cookies.clear()

    callback = await env.client.get(
        "/api/v1/auth/github/callback", params={"code": "c", "state": state}
    )

    assert callback.status_code == 401
    poll = await env.client.post(
        f"{BASE}/{data['sessionId']}/token", json={"pollSecret": data["pollSecret"]}
    )
    assert poll.json()["data"] == {"status": "PENDING"}


async def test_cli_callback_cancelled_on_github_marks_session_denied(env: Env) -> None:
    data = await _create(env.client)
    authorize = await env.client.get(f"{BASE}/{data['sessionId']}/authorize")
    state = authorize.headers["location"].split("state=")[1]

    callback = await env.client.get(
        "/api/v1/auth/github/callback", params={"error": "access_denied", "state": state}
    )
    poll = await env.client.post(
        f"{BASE}/{data['sessionId']}/token", json={"pollSecret": data["pollSecret"]}
    )

    assert callback.status_code == 200
    assert "취소" in callback.text
    assert poll.json()["data"] == {"status": "DENIED"}


async def test_web_callback_cancelled_on_github_still_redirects_to_web_login(env: Env) -> None:
    start = await env.client.get("/api/v1/auth/github")
    state = start.headers["location"].split("state=")[1]

    response = await env.client.get(
        "/api/v1/auth/github/callback", params={"error": "access_denied", "state": state}
    )

    assert response.status_code == 302
    assert response.headers["location"] == "http://web.test/login?error=access_denied"
    assert env.cli_sessions.sessions == {}


async def test_poll_with_wrong_secret_returns_unauthorized_envelope(env: Env) -> None:
    data = await _create(env.client)

    response = await env.client.post(
        f"{BASE}/{data['sessionId']}/token", json={"pollSecret": "wrong"}
    )

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"
    assert "accessToken" not in response.text


async def test_poll_unknown_session_returns_not_found(env: Env) -> None:
    response = await env.client.post(f"{BASE}/unknown/token", json={"pollSecret": "any"})

    assert response.status_code == 404


async def test_poll_without_body_returns_validation_error(env: Env) -> None:
    data = await _create(env.client)

    response = await env.client.post(f"{BASE}/{data['sessionId']}/token", json={})

    assert response.status_code == 422
    assert response.json()["details"][0]["field"] == "pollSecret"


async def test_poll_faster_than_interval_returns_429_with_retry_after(env: Env) -> None:
    data = await _create(env.client)
    poll_url = f"{BASE}/{data['sessionId']}/token"
    await env.client.post(poll_url, json={"pollSecret": data["pollSecret"]})

    response = await env.client.post(poll_url, json={"pollSecret": data["pollSecret"]})

    assert response.status_code == 429
    assert response.json()["code"] == "TOO_MANY_REQUESTS"
    assert response.headers["retry-after"] == "2"
