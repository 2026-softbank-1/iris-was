from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.enums import (
    AnalysisErrorCode,
    AnalysisGateComplexity,
    AnalysisGateDecision,
    AnalysisGateMode,
    RepositoryAnalysisStatus,
)
from app.models.base import Base, BigIntPk, TimestampMixin, enum_column


class RepositoryAnalysis(TimestampMixin, Base):
    """서비스를 만들기 전 레포 구성 분석(Analysis Gate) 1건. Build Worker 가 선점해 실행한다.

    소스는 접수할 때 고정한 커밋(source_sha)이다. 분석기 응답은 result 에 그대로 둔다.
    """

    __tablename__ = "repository_analyses"
    __table_args__ = (
        # Build Worker 가 FOR UPDATE SKIP LOCKED 로 다음 분석을 고르는 경로.
        Index(
            "ix_repository_analyses_pending",
            "created_at",
            postgresql_where=text("status IN ('QUEUED', 'RUNNING')"),
        ),
        # 푸시가 다시 접수하는 스택 재분석은 스택·커밋마다 한 번이다(웹훅 재전송 중복 방지).
        Index(
            "uq_repository_analyses_stack_id_source_sha",
            "stack_id",
            "source_sha",
            unique=True,
            postgresql_where=text("stack_id IS NOT NULL"),
        ),
    )

    id: Mapped[BigIntPk]
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    source_repository_url: Mapped[str] = mapped_column(String(500))
    github_installation_id: Mapped[int | None] = mapped_column(
        ForeignKey("github_installations.id")
    )
    source_branch: Mapped[str] = mapped_column(String(255))
    source_sha: Mapped[str | None] = mapped_column(String(64))
    # 저장소 안의 분석 위치. None 이면 저장소 루트다.
    root_directory: Mapped[str | None] = mapped_column(String(255))
    mode: Mapped[AnalysisGateMode] = mapped_column(enum_column(AnalysisGateMode, "mode"))
    status: Mapped[RepositoryAnalysisStatus] = mapped_column(
        enum_column(RepositoryAnalysisStatus, "repository_analysis_status"),
        server_default=RepositoryAnalysisStatus.QUEUED.value,
        default=RepositoryAnalysisStatus.QUEUED,
    )
    decision: Mapped[AnalysisGateDecision | None] = mapped_column(
        enum_column(AnalysisGateDecision, "decision")
    )
    complexity: Mapped[AnalysisGateComplexity | None] = mapped_column(
        enum_column(AnalysisGateComplexity, "complexity")
    )
    # 분석기 응답(iris.analysis-gate.v1) 원문.
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_code: Mapped[AnalysisErrorCode | None] = mapped_column(
        enum_column(AnalysisErrorCode, "error_code")
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    # apply 로 만든 서비스 id 목록. 같은 apply 를 다시 보내면 이 서비스들을 돌려준다.
    applied_service_ids: Mapped[list[int] | None] = mapped_column(JSONB)
    # 푸시로 다시 접수한 스택 재분석이면 그 스택. 결과가 스택과 다르면 스택에 pendingChanges 를
    # 남긴다.
    stack_id: Mapped[int | None] = mapped_column(ForeignKey("service_stacks.id", use_alter=True))
    # Worker 선점. lease 가 만료된 RUNNING 은 다른 Worker 가 다시 가져간다.
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"), default=0)
    locked_by: Mapped[str | None] = mapped_column(String(255))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def is_finished(self) -> bool:
        return self.status not in (
            RepositoryAnalysisStatus.QUEUED,
            RepositoryAnalysisStatus.RUNNING,
        )

    def succeed(
        self,
        decision: AnalysisGateDecision,
        complexity: AnalysisGateComplexity,
        result: dict[str, Any],
    ) -> None:
        self.status = RepositoryAnalysisStatus.SUCCEEDED
        self.decision = decision
        self.complexity = complexity
        self.result = result
        self.error_code = None
        self.error_message = None
        self.locked_until = None

    def fail(self, error_code: AnalysisErrorCode, error_message: str | None = None) -> None:
        self.status = RepositoryAnalysisStatus.FAILED
        self.error_code = error_code
        self.error_message = error_message
        self.locked_until = None

    def build_gate_plan(self, unit_id: str | None) -> dict[str, Any]:
        """서비스 `analysis_plan.gate` 에 남기는 분석 근거. 키는 API 와 같은 camelCase 다."""
        return {
            "analysisId": self.id,
            "decision": self.decision.value if self.decision is not None else None,
            "complexity": self.complexity.value if self.complexity is not None else None,
            "unitId": unit_id,
            "sourceSha": self.source_sha,
        }

    def mark_as_applied(self, service_ids: list[int]) -> None:
        self.status = RepositoryAnalysisStatus.APPLIED
        self.applied_service_ids = service_ids
