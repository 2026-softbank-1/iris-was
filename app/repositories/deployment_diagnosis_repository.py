from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import DiagnosisNotFoundError
from app.enums import DiagnosisStatus
from app.models.base import now_utc
from app.models.deployment_diagnosis import DeploymentDiagnosis


class DeploymentDiagnosisRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_running_if_absent(
        self, deployment_request_id: int, requested_by: int | None
    ) -> DeploymentDiagnosis | None:
        """진행 중인 진단이 이미 있으면 만들지 않고 None 이다.

        동시에 들어온 요청이 같은 제약을 두고 경쟁해도 트랜잭션이 깨지지 않게 DB 에 맡긴다.
        """
        stmt = (
            insert(DeploymentDiagnosis)
            .values(
                deployment_request_id=deployment_request_id,
                requested_by=requested_by,
                status=DiagnosisStatus.RUNNING,
            )
            .on_conflict_do_nothing()
            .returning(DeploymentDiagnosis)
        )
        return (await self._session.scalars(stmt)).one_or_none()

    async def get_by_id(self, diagnosis_id: int) -> DeploymentDiagnosis:
        """최신 값으로 읽는다. 트랜잭션을 되돌린 뒤에도 만료된 속성 없이 쓸 수 있다."""
        stmt = (
            select(DeploymentDiagnosis)
            .where(DeploymentDiagnosis.id == diagnosis_id)
            .execution_options(populate_existing=True)
        )
        diagnosis = (await self._session.scalars(stmt)).one_or_none()
        if diagnosis is None:
            raise DiagnosisNotFoundError("diagnosis not found", diagnosis_id=diagnosis_id)
        return diagnosis

    async def find_latest_by_deployment_request_id(
        self, deployment_request_id: int
    ) -> DeploymentDiagnosis | None:
        stmt = (
            select(DeploymentDiagnosis)
            .where(DeploymentDiagnosis.deployment_request_id == deployment_request_id)
            .order_by(DeploymentDiagnosis.created_at.desc(), DeploymentDiagnosis.id.desc())
            .limit(1)
        )
        return (await self._session.scalars(stmt)).one_or_none()

    async def find_latest_succeeded_by_deployment_request_id(
        self, deployment_request_id: int
    ) -> DeploymentDiagnosis | None:
        stmt = (
            select(DeploymentDiagnosis)
            .where(
                DeploymentDiagnosis.deployment_request_id == deployment_request_id,
                DeploymentDiagnosis.status == DiagnosisStatus.SUCCEEDED,
            )
            .order_by(DeploymentDiagnosis.created_at.desc(), DeploymentDiagnosis.id.desc())
            .limit(1)
        )
        return (await self._session.scalars(stmt)).one_or_none()

    async def fail_stale_running(
        self, deployment_request_id: int, started_before: datetime, error_code: str
    ) -> None:
        """오래 끝나지 않은 진행 중 진단을 실패로 닫는다.

        서버가 죽어 남은 행이 이 배포 요청의 새 진단을 영원히 막지 않게 한다.
        """
        await self._session.execute(
            update(DeploymentDiagnosis)
            .where(
                DeploymentDiagnosis.deployment_request_id == deployment_request_id,
                DeploymentDiagnosis.status == DiagnosisStatus.RUNNING,
                DeploymentDiagnosis.created_at < started_before,
            )
            .values(status=DiagnosisStatus.FAILED, error_code=error_code, finished_at=now_utc())
        )
