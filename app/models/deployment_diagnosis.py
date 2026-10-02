from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.enums import DiagnosisJobStatus
from app.models.base import Base, TimestampMixin, enum_column


class DeploymentDiagnosis(TimestampMixin, Base):
    __tablename__ = "deployment_diagnoses"
    __table_args__ = (
        UniqueConstraint("deployment_id", "attempt_id", name="uq_deployment_diagnoses_attempt"),
        Index("ix_deployment_diagnoses_latest", "deployment_id", text("created_at DESC")),
        Index("ix_deployment_diagnoses_queue", "status", "locked_until"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"))
    deployment_id: Mapped[int] = mapped_column(ForeignKey("deployment_requests.id"))
    requested_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    attempt_id: Mapped[str] = mapped_column(String(128))
    trigger: Mapped[str] = mapped_column(String(32))
    status: Mapped[DiagnosisJobStatus] = mapped_column(
        enum_column(DiagnosisJobStatus, "diagnosis_job_status"), default=DiagnosisJobStatus.QUEUED
    )
    stage: Mapped[str] = mapped_column(String(32), default="queued")
    model_selection: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    deployment_context: Mapped[dict[str, Any]] = mapped_column(JSONB)
    source_logs: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    lease_token: Mapped[str | None] = mapped_column(String(36))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
