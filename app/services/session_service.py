from datetime import timedelta

from app.core.exceptions import UnauthorizedError
from app.core.security import create_session_token, decode_session_token
from app.models.user import User
from app.repositories.user_repository import UserRepository


class SessionService:
    """세션 토큰의 발급·검증. 로그인 수단(GitHub)과 무관하게 현재 사용자를 알아낸다."""

    def __init__(
        self, user_repository: UserRepository, session_secret: str, session_ttl: timedelta
    ) -> None:
        self._user_repository = user_repository
        self._session_secret = session_secret
        self._session_ttl = session_ttl

    def create_session_token(self, user: User) -> str:
        return create_session_token(user.id, self._session_secret, self._session_ttl)

    async def get_user(self, session_token: str) -> User:
        user_id = decode_session_token(session_token, self._session_secret)
        user = await self._user_repository.find_by_id(user_id)
        if user is None:
            raise UnauthorizedError("session user not found", user_id=user_id)
        return user
