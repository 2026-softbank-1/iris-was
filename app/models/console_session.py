from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigIntPk, TimestampMixin


class ConsoleSession(TimestampMixin, Base):
    """서비스 콘솔(실행 중인 Pod 의 셸) 연결 ticket 을 발급한 1회의 감사 기록.

    쓰기만 하고 고치지 않으며 지우지 않는다. 셸 입출력은 남기지 않는다. 실제로 붙었는지·언제
    끝났는지는 Console Gateway 의 구조화 로그에 남는다.
    """

    __tablename__ = "console_sessions"
    # 서비스의 콘솔 접속 이력을 시간순으로 읽는 경로.
    __table_args__ = (
        Index("ix_console_sessions_service_id_created_at", "service_id", "created_at"),
    )

    id: Mapped[BigIntPk]
    # 응답의 sessionId 이자 ticket 의 jti(UUID). 추측 불가한 공개 ID 다.
    public_id: Mapped[str] = mapped_column(String(36), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"))
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id"))
    # 발급 시점에 떠 있던(lastKnownGood) release. 어느 배포의 Pod 에 붙으려 했는지 남긴다.
    release_id: Mapped[int] = mapped_column(ForeignKey("releases.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
