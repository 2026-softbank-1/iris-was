from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from app.core.exceptions import NotFoundError, TooManyRequestsError, UnauthorizedError
from app.core.security import hash_poll_secret, verify_oauth_state
from app.enums import CliLoginSessionStatus
from app.models.cli_login_session import CliLoginSession
from app.models.user import User
from app.services.auth_service import AuthService, CliApprovalResult
from app.services.cli_login_service import (
    CLI_LOGIN_SESSION_TTL,
    EXPIRED_SESSION_RETENTION,
    CliLoginService,
    CliLoginStart,
)
from app.services.session_service import SessionService
from tests.fakes import (
    FakeCliLoginSessionRepository,
    FakeGithubInstallationRepository,
    FakeOAuthClient,
    FakeSession,
    FakeUserRepository,
)

SECRET = "test-secret-with-enough-length-for-hs256"


@dataclass
class Env:
    cli_login_service: CliLoginService
    auth_service: AuthService
    cli_sessions: FakeCliLoginSessionRepository
    users: FakeUserRepository
    session_service: SessionService
    db_session: FakeSession
    oauth_client: FakeOAuthClient

    async def stored(self, start: CliLoginStart) -> CliLoginSession:
        cli_session = await self.cli_sessions.find_by_public_id(start.session_id)
        assert cli_session is not None
        return cli_session


def _build() -> Env:
    user = User(github_id=1001, login="octocat")
    user.id = 1
    users = FakeUserRepository([user])
    cli_sessions = FakeCliLoginSessionRepository()
    db_session = FakeSession()
    session_service = SessionService(users, SECRET, timedelta(minutes=5))  # type: ignore[arg-type]
    cli_login_service = CliLoginService(
        db_session,  # type: ignore[arg-type]
        cli_sessions,  # type: ignore[arg-type]
        users,  # type: ignore[arg-type]
        session_service,
    )
    oauth_client = FakeOAuthClient()
    auth_service = AuthService(
        db_session,  # type: ignore[arg-type]
        users,  # type: ignore[arg-type]
        FakeGithubInstallationRepository(),  # type: ignore[arg-type]
        oauth_client,
        session_service,
        cli_login_service,
        SECRET,
    )
    return Env(
        cli_login_service,
        auth_service,
        cli_sessions,
        users,
        session_service,
        db_session,
        oauth_client,
    )


async def _approve(env: Env, start: CliLoginStart) -> None:
    cli_session = await env.stored(start)
    cli_session.approve(1)


async def test_create_session_stores_only_the_poll_secret_hash() -> None:
    env = _build()

    start = await env.cli_login_service.create_session()

    cli_session = await env.stored(start)
    assert cli_session.status == CliLoginSessionStatus.PENDING
    assert cli_session.poll_secret_hash == hash_poll_secret(start.poll_secret)
    assert start.poll_secret not in (cli_session.public_id, cli_session.poll_secret_hash)
    assert cli_session.user_id is None
    assert cli_session.consumed_at is None
    assert env.db_session.commit_count == 1


async def test_create_session_expires_in_ten_minutes_and_tells_poll_interval() -> None:
    env = _build()
    before = datetime.now(UTC)

    start = await env.cli_login_service.create_session()

    cli_session = await env.stored(start)
    assert start.expires_in == 600
    assert start.interval == 2
    assert before + CLI_LOGIN_SESSION_TTL <= cli_session.expires_at
    assert cli_session.expires_at <= datetime.now(UTC) + CLI_LOGIN_SESSION_TTL


async def test_create_session_issues_distinct_unguessable_ids() -> None:
    env = _build()

    starts = [await env.cli_login_service.create_session() for _ in range(3)]

    assert len({s.session_id for s in starts}) == 3
    assert len({s.poll_secret for s in starts}) == 3
    assert all(len(s.session_id) >= 43 and len(s.poll_secret) >= 43 for s in starts)


async def test_create_session_purges_sessions_expired_long_ago_but_keeps_recent_ones() -> None:
    env = _build()
    old = await env.cli_login_service.create_session()
    recent = await env.cli_login_service.create_session()
    now = datetime.now(UTC)
    (await env.stored(old)).expires_at = now - EXPIRED_SESSION_RETENTION - timedelta(minutes=1)
    (await env.stored(recent)).expires_at = now - timedelta(minutes=1)

    await env.cli_login_service.create_session()

    assert await env.cli_sessions.find_by_public_id(old.session_id) is None
    assert await env.cli_sessions.find_by_public_id(recent.session_id) is not None


async def test_get_pending_session_returns_waiting_session() -> None:
    env = _build()
    start = await env.cli_login_service.create_session()

    cli_session = await env.cli_login_service.get_pending_session(start.session_id)

    assert cli_session.public_id == start.session_id


async def test_get_pending_session_unknown_raises_not_found() -> None:
    env = _build()

    with pytest.raises(NotFoundError):
        await env.cli_login_service.get_pending_session("unknown")


async def test_get_pending_session_expired_raises_not_found() -> None:
    env = _build()
    start = await env.cli_login_service.create_session()
    (await env.stored(start)).expires_at = datetime.now(UTC) - timedelta(seconds=1)

    with pytest.raises(NotFoundError):
        await env.cli_login_service.get_pending_session(start.session_id)


@pytest.mark.parametrize("status", [CliLoginSessionStatus.APPROVED, CliLoginSessionStatus.DENIED])
async def test_get_pending_session_already_handled_raises_not_found(
    status: CliLoginSessionStatus,
) -> None:
    env = _build()
    start = await env.cli_login_service.create_session()
    (await env.stored(start)).status = status

    with pytest.raises(NotFoundError):
        await env.cli_login_service.get_pending_session(start.session_id)


async def test_poll_pending_session_answers_pending_without_token() -> None:
    env = _build()
    start = await env.cli_login_service.create_session()

    poll = await env.cli_login_service.poll_token(start.session_id, start.poll_secret)

    assert poll.status == CliLoginSessionStatus.PENDING
    assert poll.access_token is None
    assert (await env.stored(start)).last_polled_at is not None


async def test_poll_unknown_session_raises_not_found() -> None:
    env = _build()

    with pytest.raises(NotFoundError):
        await env.cli_login_service.poll_token("unknown", "any")


async def test_poll_with_wrong_secret_raises_unauthorized_and_changes_nothing() -> None:
    env = _build()
    start = await env.cli_login_service.create_session()
    await _approve(env, start)

    with pytest.raises(UnauthorizedError):
        await env.cli_login_service.poll_token(start.session_id, "wrong-secret")

    cli_session = await env.stored(start)
    assert cli_session.status == CliLoginSessionStatus.APPROVED
    assert cli_session.consumed_at is None
    assert cli_session.last_polled_at is None


async def test_poll_approved_session_issues_session_token_of_approved_user_once() -> None:
    env = _build()
    start = await env.cli_login_service.create_session()
    await _approve(env, start)

    first = await env.cli_login_service.poll_token(start.session_id, start.poll_secret)
    second = await env.cli_login_service.poll_token(start.session_id, start.poll_secret)

    assert first.status == CliLoginSessionStatus.APPROVED
    assert first.access_token is not None
    assert (await env.session_service.get_user(first.access_token)).id == 1
    assert second.status == CliLoginSessionStatus.EXPIRED
    assert second.access_token is None
    cli_session = await env.stored(start)
    assert cli_session.consumed_at is not None


async def test_poll_denied_session_answers_denied_without_token() -> None:
    env = _build()
    start = await env.cli_login_service.create_session()
    await env.cli_login_service.deny_session(start.session_id)

    poll = await env.cli_login_service.poll_token(start.session_id, start.poll_secret)

    assert poll.status == CliLoginSessionStatus.DENIED
    assert poll.access_token is None


async def test_poll_pending_session_after_expiry_answers_expired_and_saves_it() -> None:
    env = _build()
    start = await env.cli_login_service.create_session()
    (await env.stored(start)).expires_at = datetime.now(UTC) - timedelta(seconds=1)

    poll = await env.cli_login_service.poll_token(start.session_id, start.poll_secret)

    assert poll.status == CliLoginSessionStatus.EXPIRED
    assert (await env.stored(start)).status == CliLoginSessionStatus.EXPIRED
    assert env.db_session.commit_count == 2  # 생성 + 만료 저장


async def test_poll_approved_session_after_expiry_does_not_issue_token() -> None:
    env = _build()
    start = await env.cli_login_service.create_session()
    await _approve(env, start)
    (await env.stored(start)).expires_at = datetime.now(UTC) - timedelta(seconds=1)

    poll = await env.cli_login_service.poll_token(start.session_id, start.poll_secret)

    assert poll.status == CliLoginSessionStatus.EXPIRED
    assert poll.access_token is None
    assert (await env.stored(start)).consumed_at is None


async def test_poll_locks_the_session_row_before_deciding() -> None:
    env = _build()
    start = await env.cli_login_service.create_session()

    await env.cli_login_service.poll_token(start.session_id, start.poll_secret)

    assert env.cli_sessions.for_update_lookups == 1


async def test_poll_faster_than_interval_raises_too_many_requests_with_retry_after() -> None:
    env = _build()
    start = await env.cli_login_service.create_session()
    await env.cli_login_service.poll_token(start.session_id, start.poll_secret)
    cli_session = await env.stored(start)
    polled_at = cli_session.last_polled_at

    with pytest.raises(TooManyRequestsError) as caught:
        await env.cli_login_service.poll_token(start.session_id, start.poll_secret)

    assert caught.value.retry_after_seconds == 2
    # 거절한 요청은 기준 시각을 밀지 않는다. 계속 두드려도 interval 뒤에는 통과한다.
    assert cli_session.last_polled_at == polled_at


@pytest.mark.parametrize(("elapsed_seconds", "retry_after"), [(0.2, 2), (1.0, 1)])
async def test_poll_retry_after_counts_remaining_seconds_of_interval(
    elapsed_seconds: float, retry_after: int
) -> None:
    env = _build()
    start = await env.cli_login_service.create_session()
    (await env.stored(start)).last_polled_at = datetime.now(UTC) - timedelta(
        seconds=elapsed_seconds
    )

    with pytest.raises(TooManyRequestsError) as caught:
        await env.cli_login_service.poll_token(start.session_id, start.poll_secret)

    assert caught.value.retry_after_seconds == retry_after


@pytest.mark.parametrize("elapsed_seconds", [1.6, 2.0, 5.0])
async def test_poll_at_or_near_interval_is_allowed(elapsed_seconds: float) -> None:
    # 정상 CLI 는 interval 만큼 쉬고 보낸다. 지터로 조금 일찍 도착해도 막지 않는다.
    env = _build()
    start = await env.cli_login_service.create_session()
    (await env.stored(start)).last_polled_at = datetime.now(UTC) - timedelta(
        seconds=elapsed_seconds
    )

    poll = await env.cli_login_service.poll_token(start.session_id, start.poll_secret)

    assert poll.status == CliLoginSessionStatus.PENDING


async def test_poll_right_after_approval_is_not_rate_limited() -> None:
    env = _build()
    start = await env.cli_login_service.create_session()
    await env.cli_login_service.poll_token(start.session_id, start.poll_secret)
    await _approve(env, start)

    poll = await env.cli_login_service.poll_token(start.session_id, start.poll_secret)

    assert poll.status == CliLoginSessionStatus.APPROVED
    assert poll.access_token is not None


async def test_approve_session_links_user_and_second_approval_is_rejected() -> None:
    env = _build()
    start = await env.cli_login_service.create_session()
    user = env.users.users[1]

    await env.cli_login_service.approve_session(start.session_id, user)

    cli_session = await env.stored(start)
    assert cli_session.status == CliLoginSessionStatus.APPROVED
    assert cli_session.user_id == 1
    other = User(github_id=2002, login="mallory")
    other.id = 2
    with pytest.raises(NotFoundError):
        await env.cli_login_service.approve_session(start.session_id, other)
    assert cli_session.user_id == 1


async def test_approve_session_expired_raises_not_found() -> None:
    env = _build()
    start = await env.cli_login_service.create_session()
    (await env.stored(start)).expires_at = datetime.now(UTC) - timedelta(seconds=1)

    with pytest.raises(NotFoundError):
        await env.cli_login_service.approve_session(start.session_id, env.users.users[1])

    assert (await env.stored(start)).status == CliLoginSessionStatus.PENDING


async def test_deny_session_marks_pending_session_denied_and_saves() -> None:
    env = _build()
    start = await env.cli_login_service.create_session()

    await env.cli_login_service.deny_session(start.session_id)

    assert (await env.stored(start)).status == CliLoginSessionStatus.DENIED
    assert env.db_session.commit_count == 2


async def test_deny_session_does_not_override_approved_session_or_fail_on_unknown() -> None:
    env = _build()
    start = await env.cli_login_service.create_session()
    await _approve(env, start)

    await env.cli_login_service.deny_session(start.session_id)
    await env.cli_login_service.deny_session("unknown")

    assert (await env.stored(start)).status == CliLoginSessionStatus.APPROVED


# --- AuthService: 같은 GitHub OAuth 콜백으로 CLI 로그인 승인 ---


async def test_start_cli_login_puts_cli_session_id_in_oauth_state() -> None:
    env = _build()
    start = await env.cli_login_service.create_session()

    login = await env.auth_service.start_cli_login(start.session_id)

    state = login.authorization_url.split("state=")[1]
    assert verify_oauth_state(state, login.nonce, SECRET).cli_session_id == start.session_id


async def test_start_cli_login_unknown_or_expired_session_raises_not_found() -> None:
    env = _build()
    start = await env.cli_login_service.create_session()
    (await env.stored(start)).expires_at = datetime.now(UTC) - timedelta(seconds=1)

    with pytest.raises(NotFoundError):
        await env.auth_service.start_cli_login("unknown")
    with pytest.raises(NotFoundError):
        await env.auth_service.start_cli_login(start.session_id)


async def test_complete_login_with_cli_state_approves_session_without_web_token() -> None:
    env = _build()
    start = await env.cli_login_service.create_session()
    login = await env.auth_service.start_cli_login(start.session_id)
    state = login.authorization_url.split("state=")[1]
    commits_before = env.db_session.commit_count

    result = await env.auth_service.complete_login("code-1", state, login.nonce)

    assert isinstance(result, CliApprovalResult)
    assert result.user.login == "octocat"
    assert env.db_session.commit_count == commits_before + 1
    cli_session = await env.stored(start)
    assert cli_session.status == CliLoginSessionStatus.APPROVED
    assert cli_session.user_id == result.user.id


async def test_cli_login_end_to_end_gives_cli_token_of_the_github_account() -> None:
    env = _build()
    start = await env.cli_login_service.create_session()
    login = await env.auth_service.start_cli_login(start.session_id)
    state = login.authorization_url.split("state=")[1]

    await env.auth_service.complete_login("code-1", state, login.nonce)
    poll = await env.cli_login_service.poll_token(start.session_id, start.poll_secret)

    assert poll.access_token is not None
    user = await env.session_service.get_user(poll.access_token)
    assert user.login == "octocat"


async def test_complete_login_with_cli_state_and_wrong_nonce_does_not_call_github() -> None:
    env = _build()
    start = await env.cli_login_service.create_session()
    login = await env.auth_service.start_cli_login(start.session_id)
    state = login.authorization_url.split("state=")[1]

    with pytest.raises(UnauthorizedError):
        await env.auth_service.complete_login("code-1", state, "forged")

    assert env.oauth_client.exchanged_codes == []
    assert (await env.stored(start)).status == CliLoginSessionStatus.PENDING


async def test_complete_login_with_cli_state_for_expired_session_is_not_committed() -> None:
    env = _build()
    start = await env.cli_login_service.create_session()
    login = await env.auth_service.start_cli_login(start.session_id)
    state = login.authorization_url.split("state=")[1]
    (await env.stored(start)).expires_at = datetime.now(UTC) - timedelta(seconds=1)
    commits_before = env.db_session.commit_count

    with pytest.raises(NotFoundError):
        await env.auth_service.complete_login("code-1", state, login.nonce)

    assert env.db_session.commit_count == commits_before
    assert (await env.stored(start)).status == CliLoginSessionStatus.PENDING


async def test_complete_login_with_web_state_still_issues_web_session_token() -> None:
    env = _build()
    web = env.auth_service.start_login()
    state = web.authorization_url.split("state=")[1]

    result = await env.auth_service.complete_login("code-1", state, web.nonce)

    assert not isinstance(result, CliApprovalResult)
    assert (await env.session_service.get_user(result.session_token)).login == "octocat"
    assert env.cli_sessions.sessions == {}


async def test_cancel_cli_login_denies_the_session_of_a_cli_state() -> None:
    env = _build()
    start = await env.cli_login_service.create_session()
    login = await env.auth_service.start_cli_login(start.session_id)
    state = login.authorization_url.split("state=")[1]

    cancelled = await env.auth_service.cancel_cli_login(state, login.nonce)

    assert cancelled is True
    assert (await env.stored(start)).status == CliLoginSessionStatus.DENIED


async def test_cancel_cli_login_ignores_web_state_and_unverifiable_state() -> None:
    env = _build()
    start = await env.cli_login_service.create_session()
    cli_login = await env.auth_service.start_cli_login(start.session_id)
    cli_state = cli_login.authorization_url.split("state=")[1]
    web = env.auth_service.start_login()
    web_state = web.authorization_url.split("state=")[1]

    assert await env.auth_service.cancel_cli_login(web_state, web.nonce) is False
    assert await env.auth_service.cancel_cli_login("forged", cli_login.nonce) is False
    # nonce 쿠키가 없거나 다른 브라우저의 것이면 세션을 건드리지 않는다.
    assert await env.auth_service.cancel_cli_login(cli_state, None) is False
    assert await env.auth_service.cancel_cli_login(cli_state, "other-browser") is False

    assert (await env.stored(start)).status == CliLoginSessionStatus.PENDING
