from datetime import timedelta
from typing import Any

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.enums import JobKind, JobStatus
from app.models import DeploymentRequest, Job, Project, Service

# ponytail: 스냅샷(다운로드·업로드) 동안은 lease 를 갱신하지 않는다. 5분 안에 끝난다고 본다.
#   넘기는 일이 생기면 job 처리 중 lease 를 갱신하는 heartbeat 태스크를 둔다.
LEASE_DURATION = timedelta(minutes=5)
# 선점 트랜잭션끼리 직렬화하는 advisory lock 키. 사용자별 동시 빌드 수를 정확히 세기 위해서다.
_CLAIM_LOCK_KEY = 7_331_001


class JobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim_next_job(
        self, worker_id: str, kinds: frozenset[JobKind], user_build_limit: int
    ) -> Job | None:
        """job 1건을 RUNNING 으로 바꾸고 lease 를 잡는다. lease 가 만료된 job 도 다시 가져간다.

        BUILD 는 같은 사용자의 실행 중인 BUILD 가 user_build_limit 개 미만일 때만 선점한다.
        """
        # ponytail: 선점을 전역 lock 으로 직렬화한다. 경합이 병목이 되면 사용자별 lock 으로 바꾼다.
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": _CLAIM_LOCK_KEY}
        )
        now = func.now()
        running = aliased(Job)
        running_request = aliased(DeploymentRequest)
        running_service = aliased(Service)
        running_project = aliased(Project)
        running_builds = (
            select(func.count())
            .select_from(running)
            .join(running_request, running_request.id == running.deployment_request_id)
            .join(running_service, running_service.id == running_request.service_id)
            .join(running_project, running_project.id == running_service.project_id)
            .where(
                running.kind == JobKind.BUILD,
                running.status == JobStatus.RUNNING,
                running.locked_until >= now,
                running_project.owner_id == Project.owner_id,
            )
            .scalar_subquery()
        )
        next_job_id = (
            select(Job.id)
            .join(DeploymentRequest, DeploymentRequest.id == Job.deployment_request_id)
            .join(Service, Service.id == DeploymentRequest.service_id)
            .join(Project, Project.id == Service.project_id)
            .where(
                Job.kind.in_(kinds),
                or_(
                    Job.status.in_((JobStatus.QUEUED, JobStatus.RETRY_WAIT))
                    & (Job.run_after <= now),
                    (Job.status == JobStatus.RUNNING) & (Job.locked_until < now),
                ),
                or_(Job.kind != JobKind.BUILD, running_builds < user_build_limit),
            )
            .order_by(Job.priority.desc(), Job.created_at)
            .limit(1)
            .with_for_update(of=Job, skip_locked=True)
            .scalar_subquery()
        )
        stmt = (
            update(Job)
            .where(Job.id == next_job_id)
            .values(
                status=JobStatus.RUNNING,
                locked_by=worker_id,
                locked_until=now + LEASE_DURATION,
                attempts=Job.attempts + 1,
            )
            .returning(Job)
        )
        return (await self._session.scalars(stmt)).one_or_none()

    async def renew_lease(self, job_id: int, worker_id: str) -> bool:
        """lease 를 연장한다. 다른 Worker 가 가져갔으면 False."""
        result = await self._session.execute(
            update(Job)
            .where(Job.id == job_id, Job.locked_by == worker_id, Job.status == JobStatus.RUNNING)
            .values(locked_until=func.now() + LEASE_DURATION)
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def release(self, job_id: int, delay: timedelta = timedelta(0)) -> None:
        """작업을 반납한다(종료 신호·snooze). 실패가 아니므로 시도 횟수를 되돌린다."""
        await self._update(
            job_id,
            status=JobStatus.QUEUED,
            run_after=func.now() + delay,
            attempts=Job.attempts - 1,
            locked_by=None,
            locked_until=None,
        )

    async def retry_later(self, job_id: int, error: str, delay: timedelta) -> None:
        await self._update(
            job_id,
            status=JobStatus.RETRY_WAIT,
            run_after=func.now() + delay,
            last_error=error,
            locked_by=None,
            locked_until=None,
        )

    async def mark_succeeded(self, job_id: int) -> None:
        await self._update(job_id, status=JobStatus.SUCCEEDED, locked_until=None)

    async def mark_failed(self, job_id: int, error: str) -> None:
        await self._update(job_id, status=JobStatus.FAILED, last_error=error, locked_until=None)

    async def mark_manual_intervention(self, job_id: int, error: str) -> None:
        await self._update(
            job_id, status=JobStatus.MANUAL_INTERVENTION, last_error=error, locked_until=None
        )

    async def record_external_id(self, job_id: int, external_id: str) -> None:
        await self._update(job_id, external_id=external_id)

    def add(self, job: Job) -> None:
        self._session.add(job)

    async def save(self, job: Job) -> Job:
        self._session.add(job)
        await self._session.flush()
        return job

    async def _update(self, job_id: int, **values: Any) -> None:
        await self._session.execute(update(Job).where(Job.id == job_id).values(**values))
