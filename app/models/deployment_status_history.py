from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, func
from sqlalchemy.orm import Mapped, mapped_column

from app.enums import DeploymentStatus, FailureCode
from app.models.base import Base, BigIntPk, enum_column, now_utc


class DeploymentStatusHistory(Base):
    """배포 요청의 상태 전이 1건. 쌓기만 하고 고치지 않는다."""

    __tablename__ = "deployment_status_histories"
    __table_args__ = (
        Index(
            "ix_deployment_status_histories_deployment_request_id_created_at",
            "deployment_request_id",
            "created_at",
        ),
    )

    id: Mapped[BigIntPk]
    deployment_request_id: Mapped[int] = mapped_column(ForeignKey("deployment_requests.id"))
    # 배포 요청을 만들 때 남기는 첫 행(QUEUED)은 이전 상태가 없다.
    from_status: Mapped[DeploymentStatus | None] = mapped_column(
        enum_column(DeploymentStatus, "from_status")
    )
    to_status: Mapped[DeploymentStatus] = mapped_column(enum_column(DeploymentStatus, "to_status"))
    failure_code: Mapped[FailureCode | None] = mapped_column(
        enum_column(FailureCode, "failure_code")
    )
    # 전이 시각. 단계별 소요 시간은 이 값의 차이로 계산한다.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=now_utc
    )
