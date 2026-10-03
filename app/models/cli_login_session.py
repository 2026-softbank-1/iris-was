from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.enums import CliLoginSessionStatus
from app.models.base import Base, BigIntPk, TimestampMixin, enum_column


class CliLoginSession(TimestampMixin, Base):
    """CLI 로그인 1건. 브라우저에서 GitHub 로그인을 승인하면 CLI 가 폴링으로 토큰을 받는다."""

    __tablename__ = "cli_login_sessions"
    # 만료된 세션을 지울 때 쓰는 경로.
    __table_args__ = (Index("ix_cli_login_sessions_expires_at", "expires_at"),)

    id: Mapped[BigIntPk]
    # 인증 URL 에 들어가는 추측 불가한 값. 비밀이 아니라 세션을 가리키는 주소다.
    public_id: Mapped[str] = mapped_column(String(64), unique=True)
    # 폴링 비밀의 SHA-256(hex). 평문은 CLI 만 갖고 저장하지 않는다.
    poll_secret_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[CliLoginSessionStatus] = mapped_column(
        enum_column(CliLoginSessionStatus, "cli_login_session_status"),
        default=CliLoginSessionStatus.PENDING,
    )
    # 승인한 사용자. APPROVED 가 될 때 채운다.
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # 토큰을 내준 시각. 토큰은 한 번만 내주므로 값이 있으면 다시 내주지 않는다.
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 마지막으로 폴링을 받은 시각. interval 보다 빠른 폴링을 막는 기준이다.
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def is_expired(self, now: datetime) -> bool:
        return self.expires_at <= now

    def approve(self, user_id: int) -> None:
        self.status = CliLoginSessionStatus.APPROVED
        self.user_id = user_id

    def deny(self) -> None:
        self.status = CliLoginSessionStatus.DENIED

    def expire(self) -> None:
        self.status = CliLoginSessionStatus.EXPIRED

    def consume(self, consumed_at: datetime) -> None:
        self.status = CliLoginSessionStatus.EXPIRED
        self.consumed_at = consumed_at

    def record_poll(self, polled_at: datetime) -> None:
        self.last_polled_at = polled_at
