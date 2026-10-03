from datetime import timedelta

import pytest

from app.clients.oauth_client import InstallationInfo, OAuthUser
from app.core.exceptions import UnauthorizedError
from app.models.user import User
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


def _build(
    oauth_client: FakeOAuthClient, users: FakeUserRepository | None = None
) -> tuple[AuthService, FakeUserRepository, FakeGithubInstallationRepository, FakeSession]:
    users = users or FakeUserRepository()
    installations = FakeGithubInstallationRepository()
    session = FakeSession()
    session_service = SessionService(users, SECRET, timedelta(minutes=5))  # type: ignore[arg-type]
    service = AuthService(
        session,  # type: ignore[arg-type]
        users,  # type: ignore[arg-type]
        installations,  # type: ignore[arg-type]
        oauth_client,
        session_service,
        CliLoginService(
            session,  # type: ignore[arg-type]
            FakeCliLoginSessionRepository(),  # type: ignore[arg-type]
            users,  # type: ignore[arg-type]
            session_service,
        ),
        SECRET,
    )
    return service, users, installations, session


async def test_complete_login_creates_user_and_issues_session_token() -> None:
    service, users, _, session = _build(FakeOAuthClient())
    login = service.start_login()
    state = login.authorization_url.split("state=")[1]

    result = await service.complete_login("code-1", state, login.nonce)

    assert result.user.login == "octocat"
    assert len(users.users) == 1
    assert session.commit_count == 1
    session_service = SessionService(users, SECRET, timedelta(minutes=5))  # type: ignore[arg-type]
    assert (await session_service.get_user(result.session_token)).id == result.user.id


async def test_complete_login_updates_existing_user_profile() -> None:
    existing = User(github_id=1001, login="old-name")
    existing.id = 3
    service, users, _, _ = _build(
        FakeOAuthClient(OAuthUser(1001, "new-name", None)), FakeUserRepository([existing])
    )
    login = service.start_login()

    result = await service.complete_login(
        "c", login.authorization_url.split("state=")[1], login.nonce
    )

    assert result.user.id == 3
    assert users.users[3].login == "new-name"
    assert len(users.users) == 1


async def test_complete_login_syncs_installations_to_current_github_state() -> None:
    oauth_client = FakeOAuthClient(
        installations=[
            InstallationInfo(11, "octocat", "User"),
            InstallationInfo(22, "iris-org", "Organization"),
        ]
    )
    service, _, installations, _ = _build(oauth_client)
    login = service.start_login()
    state = login.authorization_url.split("state=")[1]

    result = await service.complete_login("c", state, login.nonce)
    # 이후 GitHub 에서 하나가 사라지면 다음 로그인에서 연결도 사라진다.
    oauth_client.installations = [InstallationInfo(22, "iris-org", "Organization")]
    await service.complete_login("c", state, login.nonce)

    linked = await installations.search_by_user_id(result.user.id)
    assert [i.installation_id for i in linked] == [22]
    assert len(installations.installations) == 2


async def test_complete_login_with_wrong_nonce_does_not_call_github() -> None:
    oauth_client = FakeOAuthClient()
    service, users, _, session = _build(oauth_client)
    login = service.start_login()

    with pytest.raises(UnauthorizedError):
        await service.complete_login("c", login.authorization_url.split("state=")[1], "forged")

    assert oauth_client.exchanged_codes == []
    assert users.users == {}
    assert session.commit_count == 0


async def test_session_service_unknown_user_raises_unauthorized() -> None:
    users = FakeUserRepository()
    session_service = SessionService(users, SECRET, timedelta(minutes=5))  # type: ignore[arg-type]
    ghost = User(github_id=1, login="ghost")
    ghost.id = 99

    with pytest.raises(UnauthorizedError):
        await session_service.get_user(session_service.create_session_token(ghost))
