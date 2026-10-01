from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, func
from sqlalchemy.orm import Mapped, mapped_column

from app.enums import JobKind, JobStatus
from app.models.base import Base, TimestampMixin


class Job(TimestampMixin, Base):
    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_status_run_after", "status", "run_after"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    deployment_request_id: Mapped[int] = mapped_column(
        ForeignKey("deployment_requests.id"), index=True
    )
    kind: Mapped[JobKind]
    status: Mapped[JobStatus] = mapped_column(server_default=JobStatus.QUEUED.value)
    payload: Mapped[dict[str, Any]]
    priority: Mapped[int] = mapped_column(server_default="0")
    run_after: Mapped[datetime] = mapped_column(server_default=func.now())
    attempts: Mapped[int] = mapped_column(server_default="0")
    max_attempts: Mapped[int] = mapped_column(server_default="3")
    locked_by: Mapped[str | None]
    locked_until: Mapped[datetime | None]
    external_id: Mapped[str | None]
    last_error: Mapped[str | None]
