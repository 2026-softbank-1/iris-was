from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigIntPk, TimestampMixin


class ServiceVariable(TimestampMixin, Base):
    """서비스가 앱 컨테이너에 넘기는 환경변수 1건. 값은 암호문으로만 저장한다.

    값 대신 같은 프로젝트 다른 서비스의 연결 정보를 가리키는 참조 변수도 있다(`reference`).
    참조는 Deploy Worker 가 봉인하기 직전에 대상의 현재 값으로 푼다.
    """

    __tablename__ = "service_variables"
    __table_args__ = (
        Index("uq_service_variables_service_id_key", "service_id", "key", unique=True),
        # 값과 참조 중 정확히 하나만 있다.
        CheckConstraint(
            "(encrypted_value IS NULL) <> (reference IS NULL)", name="value_or_reference"
        ),
    )

    id: Mapped[BigIntPk]
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"))
    key: Mapped[str] = mapped_column(String(128))
    # Fernet 토큰. 평문은 응답을 만들 때만 복호화하고, 배포 요청 스냅샷에도 이 값을 그대로 복사한다.
    encrypted_value: Mapped[str | None] = mapped_column(Text)
    # {serviceId, property}. 값을 담지 않는다.
    reference: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))

    @property
    def is_reference(self) -> bool:
        return self.reference is not None
