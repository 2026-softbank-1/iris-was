from datetime import timedelta

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import AnalysisGateMode, RepositoryAnalysisStatus
from app.models.project import Project
from app.models.repository_analysis import RepositoryAnalysis


class RepositoryAnalysisRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, analysis: RepositoryAnalysis) -> RepositoryAnalysis:
        self._session.add(analysis)
        await self._session.flush()
        return analysis

    async def add_stack_analysis_if_absent(
        self,
        *,
        stack_id: int,
        project_id: int,
        user_id: int,
        source_repository_url: str,
        github_installation_id: int | None,
        source_branch: str,
        source_sha: str,
        root_directory: str | None,
    ) -> RepositoryAnalysis | None:
        """스택 재분석을 접수한다. 같은 스택·커밋이 이미 있으면(웹훅 재전송) None 이다.

        재분석은 레포가 단순해져도 단위를 비교할 수 있게 force 모드다.
        """
        stmt = (
            insert(RepositoryAnalysis)
            .values(
                project_id=project_id,
                user_id=user_id,
                source_repository_url=source_repository_url,
                github_installation_id=github_installation_id,
                source_branch=source_branch,
                source_sha=source_sha,
                root_directory=root_directory,
                mode=AnalysisGateMode.FORCE,
                status=RepositoryAnalysisStatus.QUEUED,
                stack_id=stack_id,
            )
            .on_conflict_do_nothing(
                index_elements=["stack_id", "source_sha"],
                index_where=text("stack_id IS NOT NULL"),
            )
            .returning(RepositoryAnalysis)
        )
        return (await self._session.scalars(stmt)).one_or_none()

    async def find_by_id_and_project_id(
        self, analysis_id: int, project_id: int, *, for_update: bool = False
    ) -> RepositoryAnalysis | None:
        """프로젝트 안의 분석. 삭제된 프로젝트의 분석은 없는 것으로 본다."""
        stmt = (
            select(RepositoryAnalysis)
            .join(Project, Project.id == RepositoryAnalysis.project_id)
            .where(
                RepositoryAnalysis.id == analysis_id,
                RepositoryAnalysis.project_id == project_id,
                Project.is_deleted.is_(False),
            )
        )
        if for_update:
            stmt = stmt.with_for_update(of=RepositoryAnalysis).execution_options(
                populate_existing=True
            )
        return (await self._session.scalars(stmt)).one_or_none()

    async def get_by_id(self, analysis_id: int, *, for_update: bool = False) -> RepositoryAnalysis:
        stmt = select(RepositoryAnalysis).where(RepositoryAnalysis.id == analysis_id)
        if for_update:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return (await self._session.scalars(stmt)).one()

    async def claim_next(
        self, worker_id: str, lease_duration: timedelta
    ) -> RepositoryAnalysis | None:
        """대기 중인 분석 1건을 RUNNING 으로 바꾸고 lease 를 잡는다.

        lease 가 만료된 RUNNING(처리하던 Worker 가 죽은 분석)도 다시 가져간다.
        """
        now = func.now()
        next_id = (
            select(RepositoryAnalysis.id)
            .where(
                or_(
                    RepositoryAnalysis.status == RepositoryAnalysisStatus.QUEUED,
                    (RepositoryAnalysis.status == RepositoryAnalysisStatus.RUNNING)
                    & (RepositoryAnalysis.locked_until < now),
                )
            )
            .order_by(RepositoryAnalysis.created_at, RepositoryAnalysis.id)
            .limit(1)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )
        stmt = (
            update(RepositoryAnalysis)
            .where(RepositoryAnalysis.id == next_id)
            .values(
                status=RepositoryAnalysisStatus.RUNNING,
                locked_by=worker_id,
                locked_until=now + lease_duration,
                attempts=RepositoryAnalysis.attempts + 1,
            )
            .returning(RepositoryAnalysis)
        )
        return (await self._session.scalars(stmt)).one_or_none()

    async def release(self, analysis_id: int, worker_id: str) -> None:
        """종료 신호로 처리를 멈춘 분석을 대기열로 돌린다. 실패가 아니므로 시도 횟수를 되돌린다."""
        await self._session.execute(
            update(RepositoryAnalysis)
            .where(
                RepositoryAnalysis.id == analysis_id,
                RepositoryAnalysis.locked_by == worker_id,
                RepositoryAnalysis.status == RepositoryAnalysisStatus.RUNNING,
            )
            .values(
                status=RepositoryAnalysisStatus.QUEUED,
                attempts=RepositoryAnalysis.attempts - 1,
                locked_by=None,
                locked_until=None,
            )
        )
