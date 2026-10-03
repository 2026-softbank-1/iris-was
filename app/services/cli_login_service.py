import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, TooManyRequestsError, UnauthorizedError
from app.core.security import generate_url_token, hash_poll_secret, verify_poll_secret
from app.enums import CliLoginSessionStatus
from app.models.cli_login_session import CliLoginSession
from app.models.user import User
from app.repositories.cli_login_session_repository import CliLoginSessionRepository
from app.repositories.user_repository import UserRepository
from app.services.session_service import SessionService

logger = logging.getLogger(__name__)

CLI_LOGIN_SESSION_TTL = timedelta(minutes=10)
CLI_LOGIN_POLL_INTERVAL = timedelta(seconds=2)
# CLI 가 interval 만큼 쉬고 보내도 지터로 조금 일찍 도착할 수 있다. 이 여유보다 빠를 때만 막는다.
# CLI 는 429 를 재시도하지 않고 로그인을 실패시키므로, 정상 CLI 가 걸리지 않게 넉넉히 둔다.
POLL_INTERVAL_TOLERANCE = timedelta(milliseconds=500)
# 인증 없이 만드는 세션이라 쌓이지 않게, 만료된 지 이 기간이 지난 세션은 새 세션을 만들 때 지운다.
EXPIRED_SESSION_RETENTION = timedelta(days=1)


@dataclass(frozen=True)
class CliLoginStart:
    session_id: str
    # 평문은 이 응답에서만 볼 수 있다. DB 에는 해시만 있다.
    poll_secret: str
    expires_in: int
    interval: int


@dataclass(frozen=True)
class CliLoginPoll:
    status: CliLoginSessionStatus
    # APPROVED 이고 처음 가져가는 한 번만 값이 있다.
    access_token: str | None = None


class CliLoginService:
    """CLI 로그인 세션. 브라우저에서 GitHub 로그인을 승인하면 CLI 가 폴링으로 세션 토큰을 받는다."""

    def __init__(
        self,
        session: AsyncSession,
        cli_login_session_repository: CliLoginSessionRepository,
        user_repository: UserRepository,
        session_service: SessionService,
    ) -> None:
        self._session = session
        self._cli_login_session_repository = cli_login_session_repository
        self._user_repository = user_repository
        self._session_service = session_service

    async def create_session(self) -> CliLoginStart:
        now = datetime.now(UTC)
        await self._cli_login_session_repository.delete_expired_before(
            now - EXPIRED_SESSION_RETENTION
        )

        public_id = generate_url_token()
        poll_secret = generate_url_token()
        cli_session = await self._cli_login_session_repository.save(
            CliLoginSession(
                public_id=public_id,
                poll_secret_hash=hash_poll_secret(poll_secret),
                status=CliLoginSessionStatus.PENDING,
                expires_at=now + CLI_LOGIN_SESSION_TTL,
            )
        )
        await self._session.commit()

        logger.info(
            "cli login session created",
            extra={"action": "create_session", "cli_login_session_id": cli_session.id},
        )
        return CliLoginStart(
            session_id=public_id,
            poll_secret=poll_secret,
            expires_in=int(CLI_LOGIN_SESSION_TTL.total_seconds()),
            interval=int(CLI_LOGIN_POLL_INTERVAL.total_seconds()),
        )

    async def get_pending_session(self, public_id: str) -> CliLoginSession:
        """승인을 기다리는 세션. 모르는 세션이거나 만료됐거나 이미 처리됐으면 같은 404 로 답한다."""
        cli_session = await self._cli_login_session_repository.find_by_public_id(public_id)
        if cli_session is None or not _is_pending(cli_session, datetime.now(UTC)):
            raise NotFoundError("cli login session not found or no longer pending")
        return cli_session

    async def approve_session(self, public_id: str, user: User) -> None:
        """세션을 승인하고 사용자를 연결한다. commit 은 로그인 저장과 함께 호출한 쪽이 한다."""
        cli_session = await self._cli_login_session_repository.find_by_public_id_for_update(
            public_id
        )
        if cli_session is None or not _is_pending(cli_session, datetime.now(UTC)):
            raise NotFoundError("cli login session not found or no longer pending")

        cli_session.approve(user.id)
        logger.info(
            "cli login approved",
            extra={
                "action": "approve_session",
                "cli_login_session_id": cli_session.id,
                "user_id": user.id,
            },
        )

    async def deny_session(self, public_id: str) -> None:
        """GitHub 에서 승인이 취소됐다. 없거나 이미 처리된 세션이면 아무것도 하지 않는다."""
        cli_session = await self._cli_login_session_repository.find_by_public_id_for_update(
            public_id
        )
        if cli_session is None or not _is_pending(cli_session, datetime.now(UTC)):
            return

        cli_session.deny()
        await self._session.commit()
        logger.info(
            "cli login denied",
            extra={"action": "deny_session", "cli_login_session_id": cli_session.id},
        )

    async def poll_token(self, public_id: str, poll_secret: str) -> CliLoginPoll:
        """CLI 폴링. 승인된 세션의 토큰은 처음 한 번만 내주고, 그 뒤에는 EXPIRED 로 답한다."""
        now = datetime.now(UTC)
        cli_session = await self._cli_login_session_repository.find_by_public_id_for_update(
            public_id
        )
        if cli_session is None:
            raise NotFoundError("cli login session not found")
        if not verify_poll_secret(poll_secret, cli_session.poll_secret_hash):
            raise UnauthorizedError("invalid poll secret", cli_login_session_id=cli_session.id)

        if cli_session.status in (
            CliLoginSessionStatus.PENDING,
            CliLoginSessionStatus.APPROVED,
        ) and cli_session.is_expired(now):
            cli_session.expire()

        match cli_session.status:
            case CliLoginSessionStatus.PENDING:
                _check_poll_interval(cli_session, now)
                cli_session.record_poll(now)
                await self._session.commit()
                return CliLoginPoll(CliLoginSessionStatus.PENDING)
            case CliLoginSessionStatus.APPROVED:
                return await self._issue_token(cli_session, now)
            case status:
                # DENIED·EXPIRED. 만료로 바뀐 상태도 여기서 함께 저장한다.
                await self._session.commit()
                return CliLoginPoll(status)

    async def _issue_token(self, cli_session: CliLoginSession, now: datetime) -> CliLoginPoll:
        user = (
            await self._user_repository.find_by_id(cli_session.user_id)
            if cli_session.user_id is not None
            else None
        )
        if user is None:
            raise UnauthorizedError("session user not found", cli_login_session_id=cli_session.id)

        # 토큰을 만든 뒤 소비 처리와 함께 저장한다. 같은 세션을 다시 폴링하면 EXPIRED 가 된다.
        access_token = self._session_service.create_session_token(user)
        cli_session.consume(now)
        await self._session.commit()

        logger.info(
            "cli login token issued",
            extra={
                "action": "poll_token",
                "cli_login_session_id": cli_session.id,
                "user_id": user.id,
            },
        )
        return CliLoginPoll(CliLoginSessionStatus.APPROVED, access_token)


def _is_pending(cli_session: CliLoginSession, now: datetime) -> bool:
    return cli_session.status == CliLoginSessionStatus.PENDING and not cli_session.is_expired(now)


def _check_poll_interval(cli_session: CliLoginSession, now: datetime) -> None:
    if cli_session.last_polled_at is None:
        return
    elapsed = now - cli_session.last_polled_at
    if elapsed < CLI_LOGIN_POLL_INTERVAL - POLL_INTERVAL_TOLERANCE:
        retry_after = math.ceil((CLI_LOGIN_POLL_INTERVAL - elapsed).total_seconds())
        raise TooManyRequestsError(
            "polling too fast",
            retry_after_seconds=max(retry_after, 1),
            cli_login_session_id=cli_session.id,
        )
