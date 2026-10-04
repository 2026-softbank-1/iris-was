from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Double, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigIntPk


class OnpremMetricSample(Base):
    """사용자가 등록한 온프레미스 서버가 보낸 Pod 하나의 CPU·메모리 표본 1건.

    서버의 CronJob 이 1분마다 metrics-server 값을 보낸다. 7일이 지난 행은 지운다(쌓기만 하는 기록).
    """

    __tablename__ = "onprem_metric_samples"
    __table_args__ = (
        # 서비스 메트릭 조회 경로.
        Index("ix_onprem_metric_samples_service_id_collected_at", "service_id", "collected_at"),
        # 보존 기간이 지난 행을 지우는 경로.
        Index("ix_onprem_metric_samples_collected_at", "collected_at"),
    )

    id: Mapped[BigIntPk]
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"))
    pod: Mapped[str] = mapped_column(String(253))
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    cpu_millicores: Mapped[float] = mapped_column(Double)
    memory_bytes: Mapped[int] = mapped_column(BigInteger)
