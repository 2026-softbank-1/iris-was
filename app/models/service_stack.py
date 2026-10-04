from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.enums import DeploymentTrigger, StackDeploymentStepStatus
from app.models.base import Base, BigIntPk, TimestampMixin, enum_column, now_utc


class ServiceStack(TimestampMixin, Base):
    """같은 레포(저장소·브랜치·위치) 분석에서 만든 서비스(앱 + DB) 묶음. 배포 순서와 재분석
    기준이다.

    `analysis_id` 는 마지막으로 apply 한 분석(기준)이다. 푸시 재분석 결과가 기준과 다르면
    `pending_changes` 에 남기고, 그 분석을 apply 하면 지운다.
    """

    __tablename__ = "service_stacks"
    __table_args__ = (
        Index(
            "ix_service_stacks_repository",
            "source_repository_url",
            "source_branch",
        ),
    )

    id: Mapped[BigIntPk]
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    source_repository_url: Mapped[str] = mapped_column(String(500))
    source_branch: Mapped[str] = mapped_column(String(255))
    root_directory: Mapped[str | None] = mapped_column(String(255))
    github_installation_id: Mapped[int | None] = mapped_column(
        ForeignKey("github_installations.id")
    )
    analysis_id: Mapped[int] = mapped_column(ForeignKey("repository_analyses.id"))
    # {analysisId, sourceSha, detectedAt, changes: [{type, unitId, field?, from?, to?}]}
    pending_changes: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    pending_analysis_id: Mapped[int | None] = mapped_column(ForeignKey("repository_analyses.id"))

    def record_pending_changes(
        self, analysis_id: int, source_sha: str | None, changes: list[dict[str, Any]]
    ) -> None:
        """기준보다 새 분석의 결과만 남긴다. 바뀐 것이 없으면(되돌린 변경) 지운다."""
        if self.pending_analysis_id is not None and self.pending_analysis_id > analysis_id:
            return
        if not changes:
            self.clear_pending_changes()
            return
        self.pending_analysis_id = analysis_id
        self.pending_changes = {
            "analysisId": analysis_id,
            "sourceSha": source_sha,
            "detectedAt": now_utc().isoformat(),
            "changes": changes,
        }

    def clear_pending_changes(self) -> None:
        self.pending_analysis_id = None
        self.pending_changes = None

    def rebase(self, analysis_id: int) -> None:
        """이 분석을 새 기준으로 삼는다. 그보다 오래된 변경 감지는 지운다."""
        self.analysis_id = analysis_id
        if self.pending_analysis_id is not None and self.pending_analysis_id <= analysis_id:
            self.clear_pending_changes()


class StackDeployment(TimestampMixin, Base):
    """스택의 여러 서비스를 의존 순서대로 배포하는 1회. 서비스마다 기존 배포 요청을 하나씩
    만든다."""

    __tablename__ = "stack_deployments"

    id: Mapped[BigIntPk]
    stack_id: Mapped[int] = mapped_column(ForeignKey("service_stacks.id"), index=True)
    trigger_type: Mapped[DeploymentTrigger] = mapped_column(
        enum_column(DeploymentTrigger, "deployment_trigger")
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    requested_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class StackDeploymentStep(TimestampMixin, Base):
    """스택 배포에서 서비스 하나의 배포 요청과 그 요청이 기다리는 앞 단계 요청들."""

    __tablename__ = "stack_deployment_steps"

    id: Mapped[BigIntPk]
    stack_deployment_id: Mapped[int] = mapped_column(ForeignKey("stack_deployments.id"), index=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"))
    deployment_request_id: Mapped[int] = mapped_column(
        ForeignKey("deployment_requests.id"), unique=True
    )
    # 1 = DB, 2 = DB 를 쓰는 앱, 3 = 그 앱을 쓰는 앱 … (의존 그래프의 깊이 + 1)
    step_order: Mapped[int] = mapped_column(Integer)
    # 이 요청이 시작하기 전에 SUCCEEDED 여야 하는 같은 스택 배포의 배포 요청 id.
    depends_on_deployment_request_ids: Mapped[list[int]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), default=list
    )
    status: Mapped[StackDeploymentStepStatus] = mapped_column(
        enum_column(StackDeploymentStepStatus, "stack_deployment_step_status")
    )
    # HELD 일 때 막은 앞 단계 배포 요청.
    held_by_deployment_request_id: Mapped[int | None] = mapped_column(
        ForeignKey("deployment_requests.id")
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def start(self) -> None:
        self.status = StackDeploymentStepStatus.STARTED
        self.started_at = now_utc()

    def hold(self, held_by_deployment_request_id: int) -> None:
        self.status = StackDeploymentStepStatus.HELD
        self.held_by_deployment_request_id = held_by_deployment_request_id
