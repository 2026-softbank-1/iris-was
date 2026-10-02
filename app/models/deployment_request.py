from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.enums import (
    ACTIVE_DEPLOYMENT_STATUSES,
    DeploymentStatus,
    DeploymentTrigger,
    Environment,
    FailureCode,
)
from app.models.base import Base, BigIntPk, TimestampMixin, enum_column
from app.models.service import Service

_ACTIVE_STATUS_SQL = ", ".join(f"'{status}'" for status in ACTIVE_DEPLOYMENT_STATUSES)


class DeploymentRequest(TimestampMixin, Base):
    """사용자의 배포 요청 1건. 빌드부터 배포 완료까지 흐름의 최상위 단위다."""

    __tablename__ = "deployment_requests"
    __table_args__ = (
        # 서비스·환경마다 진행 중인 배포는 하나만 허용한다.
        Index(
            "uq_deployment_requests_active",
            "service_id",
            "environment",
            unique=True,
            postgresql_where=f"status IN ({_ACTIVE_STATUS_SQL})",
        ),
        Index("ix_deployment_requests_service_id_created_at", "service_id", "created_at"),
    )

    id: Mapped[BigIntPk]
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"))
    environment: Mapped[Environment] = mapped_column(enum_column(Environment, "environment"))
    source_sha: Mapped[str] = mapped_column(String(64))
    source_commit_message: Mapped[str | None] = mapped_column(Text)
    trigger_type: Mapped[DeploymentTrigger] = mapped_column(
        enum_column(DeploymentTrigger, "deployment_trigger")
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    # 푸시 웹훅처럼 사용자가 없는 요청은 None 이다.
    requested_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    status: Mapped[DeploymentStatus] = mapped_column(
        enum_column(DeploymentStatus, "deployment_status"), default=DeploymentStatus.QUEUED
    )
    failure_code: Mapped[FailureCode | None] = mapped_column(
        enum_column(FailureCode, "failure_code")
    )
    # 요청 시점의 환경변수. 같은 값으로 다시 배포하거나 되돌릴 때 쓴다.
    variables_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # 더 새로운 요청이 이 요청을 대신하면 기록한다. Worker 가 보고 SUPERSEDED 로 끝낸다.
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    service: Mapped[Service] = relationship(lazy="raise")

    def transition_to(self, to_status: DeploymentStatus, failure_code: FailureCode | None) -> None:
        """허용 여부는 DeploymentStatusService 가 검사한다. 상태는 이 메서드로만 바꾼다."""
        self.status = to_status
        if failure_code is not None:
            self.failure_code = failure_code
