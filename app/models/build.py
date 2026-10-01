from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.enums import Builder
from app.models.base import Base, BigIntPk, TimestampMixin, enum_column


class Build(TimestampMixin, Base):
    """배포 요청 1건당 한 번 하는 이미지 빌드. 결과는 image digest 로 남긴다."""

    __tablename__ = "builds"

    id: Mapped[BigIntPk]
    deployment_request_id: Mapped[int] = mapped_column(
        ForeignKey("deployment_requests.id"), unique=True
    )
    builder: Mapped[Builder] = mapped_column(enum_column(Builder, "builder"))
    codebuild_build_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    image_repository: Mapped[str | None] = mapped_column(String(500))
    # `sha256:...` 값만 담는다. tag 필드는 두지 않는다.
    image_digest: Mapped[str | None] = mapped_column(String(80))
    log_url: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
