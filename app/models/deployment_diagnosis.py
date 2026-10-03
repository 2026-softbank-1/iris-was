from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.enums import DiagnosisStatus
from app.models.base import Base, BigIntPk, TimestampMixin, enum_column, now_utc


class DeploymentDiagnosis(TimestampMixin, Base):
    """배포 요청 1건을 AI 로 진단한 기록 1회. 진단 결과로 배포 요청의 상태를 바꾸지 않는다.

    다시 진단하면 새 행이 쌓이고, 조회는 가장 최근 행을 본다.
    """

    __tablename__ = "deployment_diagnoses"
    __table_args__ = (
        Index(
            "ix_deployment_diagnoses_deployment_request_id_created_at",
            "deployment_request_id",
            "created_at",
        ),
        # 배포 요청마다 진행 중인 진단은 하나다. 모델 호출 비용이 겹쳐 나가지 않게 DB 가 막는다.
        Index(
            "uq_deployment_diagnoses_deployment_request_id_running",
            "deployment_request_id",
            unique=True,
            postgresql_where=text("status = 'RUNNING'"),
        ),
    )

    id: Mapped[BigIntPk]
    deployment_request_id: Mapped[int] = mapped_column(ForeignKey("deployment_requests.id"))
    requested_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    status: Mapped[DiagnosisStatus] = mapped_column(
        enum_column(DiagnosisStatus, "diagnosis_status"), default=DiagnosisStatus.RUNNING
    )
    # 에이전트가 돌려준 진단 결과(diagnosis-result.v3)를 그대로 담는다. 성공했을 때만 있다.
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # 실패 사유. 에이전트가 준 코드(MODEL_TIMEOUT 등)이거나 이 서버가 정한 코드다.
    error_code: Mapped[str | None] = mapped_column(String(64))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def succeed(self, result: dict[str, Any]) -> None:
        self.result = result
        self.status = DiagnosisStatus.SUCCEEDED
        self.finished_at = now_utc()

    def fail(self, error_code: str) -> None:
        self.error_code = error_code
        self.status = DiagnosisStatus.FAILED
        self.finished_at = now_utc()
