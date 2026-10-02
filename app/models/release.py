from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.enums import Environment, ReleaseStatus
from app.models.base import Base, BigIntPk, TimestampMixin, enum_column


class Release(TimestampMixin, Base):
    """타깃 하나에 반영한 배포 결과. 같은 이미지를 타깃마다 한 건씩 남긴다."""

    __tablename__ = "releases"
    __table_args__ = (
        # service + environment + target 의 lastKnownGood 조회 경로.
        Index("ix_releases_last_known_good", "service_id", "environment", "target_id", "status"),
    )

    id: Mapped[BigIntPk]
    deployment_request_id: Mapped[int] = mapped_column(
        ForeignKey("deployment_requests.id"), index=True
    )
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"))
    environment: Mapped[Environment] = mapped_column(enum_column(Environment, "environment"))
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id"))
    image_digest: Mapped[str] = mapped_column(String(80))
    gitops_commit_sha: Mapped[str | None] = mapped_column(String(64))
    image_repository: Mapped[str | None] = mapped_column(String(500))
    revert_commit_sha: Mapped[str | None] = mapped_column(String(64))
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Argo CD 가 주는 값을 원본 표기 그대로 저장한다 (Synced · Healthy 등).
    argo_sync_status: Mapped[str | None] = mapped_column(String(32))
    argo_health_status: Mapped[str | None] = mapped_column(String(32))
    previous_good_release_id: Mapped[int | None] = mapped_column(ForeignKey("releases.id"))
    status: Mapped[ReleaseStatus] = mapped_column(
        enum_column(ReleaseStatus, "release_status"), default=ReleaseStatus.PENDING
    )
