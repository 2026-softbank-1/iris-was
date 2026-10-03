from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigIntPk, TimestampMixin, now_utc


class DeploymentRepair(TimestampMixin, Base):
    """Frozen diagnosis/source repair attempt; generation never changes deployment state."""

    __tablename__ = "deployment_repairs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('RUNNING', 'SUCCEEDED', 'FAILED', 'UNKNOWN_OUTCOME')", name="repair_status"
        ),
        Index(
            "uq_deployment_repairs_service_id_idempotency_key",
            "service_id",
            "idempotency_key",
            unique=True,
        ),
        Index(
            "uq_deployment_repairs_deployment_request_id_running",
            "deployment_request_id",
            unique=True,
            postgresql_where=text("status = 'RUNNING'"),
        ),
    )

    id: Mapped[BigIntPk]
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"))
    deployment_request_id: Mapped[int] = mapped_column(ForeignKey("deployment_requests.id"))
    diagnosis_id: Mapped[int] = mapped_column(ForeignKey("deployment_diagnoses.id"))
    requested_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    input_digest: Mapped[str] = mapped_column(String(64))
    source_sha: Mapped[str] = mapped_column(String(40))
    source_repository_url: Mapped[str] = mapped_column(String(500))
    root_directory: Mapped[str] = mapped_column(String(255))
    plan_ids: Mapped[list[str]] = mapped_column(JSONB)
    diagnosis_result: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # Contains only server policy and pinned snapshot metadata, never download credentials.
    request_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(32), default="RUNNING")
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(64))
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    generation_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def agent_request_id(self) -> str:
        return f"was-repair-{self.id}"

    def finish(
        self, status: str, *, result: dict[str, Any] | None = None, error_code: str | None = None
    ) -> None:
        self.status = status
        self.result = result
        self.error_code = error_code
        self.finished_at = now_utc()
