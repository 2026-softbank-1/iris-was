from collections.abc import Collection
from datetime import datetime

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import DiagnosisNotFoundError
from app.enums import (
    PRE_EXECUTION_FAILURE_CODES,
    DeploymentStatus,
    DeploymentTrigger,
    DiagnosisStatus,
    ServiceKind,
)
from app.models.base import now_utc
from app.models.deployment_diagnosis import DeploymentDiagnosis
from app.models.deployment_request import DeploymentRequest
from app.models.project import Project
from app.models.service import Service


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

    async def count_running_since(self, started_after: datetime) -> int:
        """`started_after` 이후에 시작해 아직 진행 중인 진단 수. 오래 남은 행은 세지 않는다."""
        stmt = select(func.count()).where(
            DeploymentDiagnosis.status == DiagnosisStatus.RUNNING,
            DeploymentDiagnosis.created_at >= started_after,
        )
        return (await self._session.scalar(stmt)) or 0

    async def find_next_auto_start_candidate(
        self,
        failed_after: datetime,
        stale_before: datetime,
        statuses: Collection[DeploymentStatus],
    ) -> DeploymentRequest | None:
        """서버가 자동으로 진단할 다음 배포 요청. 가장 오래 기다린 것부터다.

        `failed_after` 이후에 끝난 `statuses` 의 요청 중 진단 기록이 없는 것이다. 이미 끝난
        진단(성공·실패)이나 막 시작한 진행 중 진단이 있으면 고르지 않는다. `stale_before`
        이전에 시작하고 끝나지 않은 진행 중 행만 있으면(서버가 죽어 남은 것) 다시 고른다.
        `REMOVE` 요청, 시작 전에 끝난 실패(VARIABLES_INVALID·DEPENDENCY_FAILED), 관리형 DB,
        삭제된 서비스·프로젝트의 요청은 진단하지 않는다.
        """
        has_blocking_diagnosis = (
            select(DeploymentDiagnosis.id)
            .where(
                DeploymentDiagnosis.deployment_request_id == DeploymentRequest.id,
                or_(
                    DeploymentDiagnosis.status != DiagnosisStatus.RUNNING,
                    DeploymentDiagnosis.created_at >= stale_before,
                ),
            )
            .exists()
        )
        stmt = (
            select(DeploymentRequest)
            .join(Service, Service.id == DeploymentRequest.service_id)
            .join(Project, Project.id == Service.project_id)
            .where(
                DeploymentRequest.status.in_(statuses),
                DeploymentRequest.trigger_type != DeploymentTrigger.REMOVE,
                # 빌드·배포를 시작하기 전에 끝난 실패(환경변수 검증·앞 단계 실패)는 로그가 없다.
                or_(
                    DeploymentRequest.failure_code.is_(None),
                    DeploymentRequest.failure_code.not_in(PRE_EXECUTION_FAILURE_CODES),
                ),
                # 관리형 DB 는 사용자 소스가 없어 진단할 코드가 없다.
                Service.kind == ServiceKind.APP,
                DeploymentRequest.updated_at >= failed_after,
                Service.is_deleted.is_(False),
                Project.is_deleted.is_(False),
                ~has_blocking_diagnosis,
            )
            .order_by(DeploymentRequest.updated_at, DeploymentRequest.id)
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
