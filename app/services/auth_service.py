import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.oauth_client import InstallationInfo, OAuthClient, OAuthUser
from app.core.security import create_oauth_state, verify_oauth_state
from app.models.user import GithubInstallation, User
from app.repositories.github_installation_repository import GithubInstallationRepository
from app.repositories.user_repository import UserRepository
from app.services.session_service import SessionService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LoginStart:
    authorization_url: str
    # 브라우저 쿠키에 두고, 콜백에서 state 안의 값과 맞춰본다.
    nonce: str


@dataclass(frozen=True)
class LoginResult:
    user: User
    session_token: str


class AuthService:
    """GitHub 로그인. 로그인할 때마다 사용자가 접근할 수 있는 GitHub App 설치도 맞춘다."""

    def __init__(
        self,
        session: AsyncSession,
        user_repository: UserRepository,
        installation_repository: GithubInstallationRepository,
        oauth_client: OAuthClient,
        session_service: SessionService,
        state_secret: str,
    ) -> None:
        self._session = session
        self._user_repository = user_repository
        self._installation_repository = installation_repository
        self._oauth_client = oauth_client
        self._session_service = session_service
        self._state_secret = state_secret

    def start_login(self) -> LoginStart:
        state, nonce = create_oauth_state(self._state_secret)
        return LoginStart(self._oauth_client.build_authorization_url(state), nonce)

    def start_installation(self, app_slug: str) -> LoginStart:
        """App 설치 페이지로 보낸다. 설치가 끝나면 로그인 콜백으로 돌아와 설치 목록이 맞춰진다."""
        state, nonce = create_oauth_state(self._state_secret)
        return LoginStart(self._oauth_client.build_installation_url(app_slug, state), nonce)

    async def complete_login(self, code: str, state: str, nonce: str | None) -> LoginResult:
        verify_oauth_state(state, nonce, self._state_secret)

        # 사용자 토큰은 이 함수 안에서만 쓰고 저장하지 않는다.
        access_token = await self._oauth_client.exchange_code(code)
        profile = await self._oauth_client.fetch_user(access_token)
        installations = await self._oauth_client.fetch_installations(access_token)

        user = await self._save_user(profile)
        await self._sync_installations(user, installations)
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
