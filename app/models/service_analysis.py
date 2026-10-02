from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.enums import AnalysisJobStatus
from app.models.base import Base, TimestampMixin, enum_column


class ServiceAnalysis(TimestampMixin, Base):
    """고정 소스에 대한 분석 작업. 배포 작업과 상태·결과를 따로 보관한다."""

    __tablename__ = "service_analyses"
    __table_args__ = (
        Index("ix_service_analyses_latest", "service_id", text("created_at DESC")),
        Index("ix_service_analyses_queue", "status", "locked_until"),
        Index(
            "uq_service_analyses_active",
            "service_id",
            unique=True,
            postgresql_where=text("status IN ('QUEUED', 'RUNNING')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"))
    requested_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    source_repository_url: Mapped[str] = mapped_column(String(500))
    source_branch: Mapped[str] = mapped_column(String(255))
    source_sha: Mapped[str] = mapped_column(String(40))
    root_directory: Mapped[str] = mapped_column(String(255))
    # GitHub의 외부 installation ID를 접수 시 고정하며 로컬 설치 PK가 아니다.
    github_installation_id: Mapped[int] = mapped_column(BigInteger)
    mode: Mapped[str] = mapped_column(String(12))
    model_selection: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[AnalysisJobStatus] = mapped_column(
        enum_column(AnalysisJobStatus, "analysis_job_status"),
        server_default=text("'QUEUED'"),
        default=AnalysisJobStatus.QUEUED,
    )
    stage: Mapped[str] = mapped_column(
        String(64), server_default=text("'queued'"), default="queued"
    )
    source_snapshot_id: Mapped[str | None] = mapped_column(String(128))
    context_hash: Mapped[str | None] = mapped_column(String(128))
    result_digest: Mapped[str | None] = mapped_column(String(128))
    analysis_status: Mapped[str | None] = mapped_column(String(32))
    analysis_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    verification_report: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    source_readiness: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    deployment_dossier: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    run_report: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    evidence: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    builder_recommendation: Mapped[str | None] = mapped_column(String(32))
    review_required: Mapped[bool] = mapped_column(server_default=text("true"), default=True)
    error_code: Mapped[str | None] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"), default=0)
    lease_token: Mapped[str | None] = mapped_column(String(36))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    selected_service_candidate_id: Mapped[str | None] = mapped_column(String(255))
