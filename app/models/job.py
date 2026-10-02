from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.enums import JobKind, JobStatus
from app.models.base import Base, BigIntPk, TimestampMixin, enum_column, now_utc


class Job(TimestampMixin, Base):
    """PostgreSQL 기반 큐의 작업 1건. 전달 보장은 at-least-once 다."""

    __tablename__ = "jobs"
    __table_args__ = (
        # Worker 가 FOR UPDATE SKIP LOCKED 로 다음 작업을 고르는 경로.
        Index(
            "ix_jobs_queued",
            text("priority DESC"),
            "created_at",
            postgresql_where=text("status = 'QUEUED'"),
        ),
    )

    id: Mapped[BigIntPk]
    deployment_request_id: Mapped[int] = mapped_column(
        ForeignKey("deployment_requests.id"), index=True
    )
    kind: Mapped[JobKind] = mapped_column(enum_column(JobKind, "job_kind"))
    status: Mapped[JobStatus] = mapped_column(
        enum_column(JobStatus, "job_status"), default=JobStatus.QUEUED
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    priority: Mapped[int] = mapped_column(Integer, server_default=text("0"), default=0)
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=now_utc
    )
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"), default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, server_default=text("5"), default=5)
    locked_by: Mapped[str | None] = mapped_column(String(255))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # CodeBuild 빌드 ID·commit SHA 같은 외부 작업 ID. 외부 호출 직후 먼저 기록한다.
    external_id: Mapped[str | None] = mapped_column(String(255))
    last_error: Mapped[str | None] = mapped_column(Text)
