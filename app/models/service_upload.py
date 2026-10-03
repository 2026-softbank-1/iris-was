from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigIntPk, TimestampMixin


class ServiceUpload(TimestampMixin, Base):
    """`likelion up` 이 올린 소스 아카이브 1건. 아카이브는 S3 에 두고 이 행은 메타데이터만 담는다.

    배포 요청 하나에만 쓰인다(`consumed_at`). 만료됐거나 쓰인 업로드는 다시 쓰지 않는다.
    """

    __tablename__ = "service_uploads"
    # 쓰이지 못하고 만료된 행을 지울 때 쓰는 경로.
    __table_args__ = (Index("ix_service_uploads_expires_at", "expires_at"),)

    id: Mapped[BigIntPk]
    # 업로드 응답과 배포 요청이 가리키는 추측 불가한 값(256비트 무작위).
    public_id: Mapped[str] = mapped_column(String(64), unique=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"))
    uploaded_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    # 올라온 아카이브(gzip)의 바이트 수와 sha256(hex). Build Worker 가 내려받으며 다시 확인한다.
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    # 아카이브가 있는 버킷 안의 키. 버킷은 설정이 정한다.
    storage_key: Mapped[str] = mapped_column(String(255))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # 배포 요청이 이 업로드를 가져간 시각. 값이 있으면 다시 쓸 수 없다.
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def is_expired(self, now: datetime) -> bool:
        return self.expires_at <= now
