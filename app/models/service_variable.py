from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigIntPk, TimestampMixin


class ServiceVariable(TimestampMixin, Base):
    """서비스가 앱 컨테이너에 넘기는 환경변수 1건. 값은 암호문으로만 저장한다."""

    __tablename__ = "service_variables"
    __table_args__ = (
        Index("uq_service_variables_service_id_key", "service_id", "key", unique=True),
    )

    id: Mapped[BigIntPk]
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"))
    key: Mapped[str] = mapped_column(String(128))
    # Fernet 토큰. 평문은 응답을 만들 때만 복호화하고, 배포 요청 스냅샷에도 이 값을 그대로 복사한다.
    encrypted_value: Mapped[str] = mapped_column(Text)
