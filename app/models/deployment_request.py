from datetime import datetime

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.enums import DeploymentStatus, DeploymentTrigger, Environment, FailureCode
from app.models.base import Base, TimestampMixin
from app.models.service import Service


class DeploymentRequest(TimestampMixin, Base):
    __tablename__ = "deployment_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"), index=True)
    environment: Mapped[Environment] = mapped_column(server_default=Environment.PROD.value)
    trigger: Mapped[DeploymentTrigger]
    # 수동 배포는 비워 두고, Build Worker 가 default 브랜치 HEAD 로 확정한다.
    source_sha: Mapped[str | None]
    idempotency_key: Mapped[str] = mapped_column(unique=True)
    requested_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    status: Mapped[DeploymentStatus] = mapped_column(server_default=DeploymentStatus.QUEUED.value)
    failure_code: Mapped[FailureCode | None]
    # 같은 서비스에 새 요청이 들어오면 Control API 가 기록한다. Worker 가 보고 중단한다.
    cancel_requested_at: Mapped[datetime | None]

    service: Mapped[Service] = relationship(lazy="raise")

    def finish(self, status: DeploymentStatus, failure_code: FailureCode | None = None) -> None:
        self.status = status
        if failure_code is not None:
            self.failure_code = failure_code
