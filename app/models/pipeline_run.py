from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.enums import DeploymentTrigger, PipelineStatus
from app.models.base import Base, TimestampMixin, enum_column


class PipelineRun(TimestampMixin, Base):
    __tablename__ = "pipeline_runs"
    __table_args__ = (
        Index("ix_pipeline_runs_latest", "service_id", text("created_at DESC")),
        Index("ix_pipeline_runs_queue", "status", "locked_until"),
        Index(
            "uq_pipeline_runs_active",
            "service_id",
            unique=True,
            postgresql_where=text(
                "status IN ('ANALYZING','AWAITING_INPUT','PLANNING','BUILDING','DEPLOYING')"
            ),
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"))
    requested_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    source_repository_url: Mapped[str] = mapped_column(String(500))
    source_branch: Mapped[str] = mapped_column(String(255))
    source_sha: Mapped[str] = mapped_column(String(40))
    root_directory: Mapped[str] = mapped_column(String(255))
    github_installation_id: Mapped[int] = mapped_column(BigInteger)
    mode: Mapped[str] = mapped_column(String(12))
    trigger_type: Mapped[DeploymentTrigger] = mapped_column(
        enum_column(DeploymentTrigger, "deployment_trigger")
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    auto_deploy: Mapped[bool] = mapped_column(default=True)
    enable_auto_deploy: Mapped[bool] = mapped_column(default=True)
    status: Mapped[PipelineStatus] = mapped_column(
        enum_column(PipelineStatus, "pipeline_status"), default=PipelineStatus.QUEUED
    )
    stage: Mapped[str] = mapped_column(String(64), default="queued")
    analysis_id: Mapped[str | None] = mapped_column(ForeignKey("service_analyses.id"))
    deployment_request_id: Mapped[int | None] = mapped_column(ForeignKey("deployment_requests.id"))
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    target_ids: Mapped[list[int]] = mapped_column(JSONB)
    confirmed_inputs: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    questions: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    deployment_dossier: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    planning_report: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    execution_plan: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    plan_digest: Mapped[str | None] = mapped_column(String(64))
    model_selection: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    lease_token: Mapped[str | None] = mapped_column(String(36))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
