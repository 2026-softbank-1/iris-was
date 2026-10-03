import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.oauth_client import InstallationInfo, OAuthClient, OAuthUser
from app.core.exceptions import UnauthorizedError
from app.core.security import create_cli_oauth_state, create_oauth_state, verify_oauth_state
from app.models.user import GithubInstallation, User
from app.repositories.github_installation_repository import GithubInstallationRepository
from app.repositories.user_repository import UserRepository
from app.services.cli_login_service import CliLoginService
from app.services.session_service import SessionService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LoginStart:
    authorization_url: str
    # 브라우저 쿠키에 두고, 콜백에서 state 안의 값과 맞춰본다.
    nonce: str


@dataclass(frozen=True)
class LoginResult:
    """웹 로그인 결과. 세션 토큰을 쿠키로 내려 준다."""

    user: User
    session_token: str


@dataclass(frozen=True)
class CliApprovalResult:
    """CLI 로그인 승인 결과. 토큰은 브라우저가 아니라 CLI 가 폴링으로 받아 가므로 담지 않는다."""

    user: User


class AuthService:
    """GitHub 로그인. 로그인할 때마다 사용자가 접근할 수 있는 GitHub App 설치도 맞춘다.

    같은 OAuth 콜백이 웹 로그인과 CLI 로그인 승인을 모두 끝낸다. 어느 쪽인지는 state 가 정한다.
    """

    def __init__(
        self,
        session: AsyncSession,
        user_repository: UserRepository,
        installation_repository: GithubInstallationRepository,
        oauth_client: OAuthClient,
        session_service: SessionService,
        cli_login_service: CliLoginService,
        state_secret: str,
    ) -> None:
        self._session = session
        self._user_repository = user_repository
        self._installation_repository = installation_repository
        self._oauth_client = oauth_client
        self._session_service = session_service
        self._cli_login_service = cli_login_service
        self._state_secret = state_secret

    def start_login(self) -> LoginStart:
        state, nonce = create_oauth_state(self._state_secret)
        return LoginStart(self._oauth_client.build_authorization_url(state), nonce)

    def start_installation(self, app_slug: str) -> LoginStart:
        """App 설치 페이지로 보낸다. 설치가 끝나면 로그인 콜백으로 돌아와 설치 목록이 맞춰진다."""
        state, nonce = create_oauth_state(self._state_secret)
        return LoginStart(self._oauth_client.build_installation_url(app_slug, state), nonce)

    async def start_cli_login(self, cli_session_id: str) -> LoginStart:
        """CLI 로그인 세션을 승인하러 온 브라우저를 GitHub 로그인으로 보낸다."""
        await self._cli_login_service.get_pending_session(cli_session_id)
        state, nonce = create_cli_oauth_state(cli_session_id, self._state_secret)
        return LoginStart(self._oauth_client.build_authorization_url(state), nonce)

    async def cancel_cli_login(self, state: str, nonce: str | None) -> bool:
        """GitHub 에서 로그인이 취소·실패했을 때 CLI 로그인 세션을 거절로 바꾼다.

        CLI 승인 흐름이었으면 True. 웹 로그인이거나 검증할 수 없는 state 면 세션을 건드리지 않고
        False 를 돌려, 호출한 쪽이 웹 로그인 실패로 처리하게 한다.
        """
        try:
            oauth_state = verify_oauth_state(state, nonce, self._state_secret)
        except UnauthorizedError:
            return False
        if oauth_state.cli_session_id is None:
            return False
        await self._cli_login_service.deny_session(oauth_state.cli_session_id)
        return True

    async def complete_login(
        self, code: str, state: str, nonce: str | None
    ) -> LoginResult | CliApprovalResult:
        oauth_state = verify_oauth_state(state, nonce, self._state_secret)

        # 사용자 토큰은 이 함수 안에서만 쓰고 저장하지 않는다.
        access_token = await self._oauth_client.exchange_code(code)
        profile = await self._oauth_client.fetch_user(access_token)
        installations = await self._oauth_client.fetch_installations(access_token)

        user = await self._save_user(profile)
        await self._sync_installations(user, installations)
        if oauth_state.cli_session_id is not None:
            # 사용자 저장과 세션 승인을 한 트랜잭션으로 반영한다. 세션이 만료됐으면 둘 다 취소된다.
            await self._cli_login_service.approve_session(oauth_state.cli_session_id, user)
            await self._session.commit()
            return CliApprovalResult(user)
        await self._session.commit()

        logger.info(
            "github login completed",
            extra={
                "action": "complete_login",
                "user_id": user.id,
                "installation_count": len(installations),
            },
        )
        return LoginResult(user, self._session_service.create_session_token(user))

    async def _save_user(self, profile: OAuthUser) -> User:
        user = await self._user_repository.find_by_github_id(profile.github_id)
        if user is None:
            user = User(github_id=profile.github_id, login=profile.login)
        user.login = profile.login
        user.avatar_url = profile.avatar_url
        return await self._user_repository.save(user)

    async def _sync_installations(self, user: User, installations: list[InstallationInfo]) -> None:
        saved_ids: set[int] = set()
        for info in installations:
            installation = await self._installation_repository.find_by_installation_id(
                info.installation_id
            )
            if installation is None:
                installation = GithubInstallation(installation_id=info.installation_id)
            installation.account_login = info.account_login
            installation.account_type = info.account_type
            saved = await self._installation_repository.save(installation)
            saved_ids.add(saved.id)
        await self._installation_repository.replace_user_links(user.id, saved_ids)
