"""BuildService·JobRepository 통합 테스트. TEST_DATABASE_URL 의 로컬 PostgreSQL 이 필요하다.

`alembic upgrade head` 가 끝난 DB 여야 한다. 데이터 테이블을 비우므로 개발 DB 가 아닌
전용 DB 를 쓴다.
"""

import asyncio
import io
import tarfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.aws_clients import CodeBuildResult
from app.core.config import BuildWorkerSettings
from app.core.exceptions import BuildFailedError, ExternalError
from app.enums import (
    BuildStatus,
    DeploymentStatus,
    DeploymentTrigger,
    Environment,
    FailureCode,
    JobKind,
    JobStatus,
)
from app.models import Build, DeploymentRequest, DeploymentStatusHistory, Job
from app.repositories.build_repository import BuildRepository
from app.repositories.deployment_request_repository import DeploymentRequestRepository
from app.repositories.deployment_status_history_repository import (
    DeploymentStatusHistoryRepository,
)
from app.repositories.job_repository import JobRepository
from app.repositories.service_variable_repository import ServiceVariableRepository
from app.services.build_service import BuildService
from app.services.deployment_request_service import DeploymentRequestService
from tests.worker_support import (
    add,
    requires_database,
    seed_service,
    session_factory_with_clean_data,
)

pytestmark = [pytest.mark.integration, requires_database]

REPOSITORY_URI = "123.dkr.ecr.ap-northeast-2.amazonaws.com/iris/services/1"
SETTINGS = BuildWorkerSettings(
    github_app_id=1,
    github_app_private_key="unused",
    aws_region="ap-northeast-2",
    codebuild_project="iris-test-build",
    artifact_bucket="iris-test-artifacts",
    poll_interval_seconds=0.01,
)


class FakeGitHub:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files

    async def create_installation_token(
        self, installation_id: int, repository_name: str | None
    ) -> str:
        return "token"

    async def get_branch_sha(self, token: str, full_name: str, branch: str | None = None) -> str:
        return "a" * 40

    async def download_tarball(
        self, token: str, full_name: str, sha: str, dest: Path, max_bytes: int
    ) -> None:
        with tarfile.open(dest, "w:gz") as archive:
            for name, content in self.files.items():
                info = tarfile.TarInfo(f"owner-repo-{sha[:7]}/{name}")
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))


class FakeCodeBuild:
    def __init__(self, *results: CodeBuildResult) -> None:
        self.results = list(results)
        self.started_envs: list[dict[str, str]] = []
        self.stopped: list[str] = []
        self.started = asyncio.Event()

    async def start_build(
        self, env: dict[str, str], idempotency_token: str, timeout_minutes: int
    ) -> str:
        self.started_envs.append(env)
        self.started.set()
        return f"iris-build:{len(self.started_envs)}"

    async def get_build(self, build_id: str) -> CodeBuildResult:
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]

    async def stop_build(self, build_id: str) -> None:
        self.stopped.append(build_id)


class FakeEcr:
    async def ensure_repository(self, service_id: int) -> str:
        return REPOSITORY_URI

    async def get_image_digest(self, repository_name: str, image_tag: str) -> str:
        return "sha256:" + "d" * 64


class FakeArtifacts:
    async def put_snapshot(self, build_id: int, path: Path) -> str:
        return f"snapshots/{build_id}.tar.gz"

    async def presign(self, key: str) -> str:
        return f"https://s3.example/{key}"


IN_PROGRESS = CodeBuildResult("IN_PROGRESS", None, None)
SUCCEEDED = CodeBuildResult("SUCCEEDED", None, "https://logs.example")


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async for factory in session_factory_with_clean_data():
        yield factory


async def _seed(
    session_factory: async_sessionmaker[AsyncSession],
    owner_github_id: int = 1,
    request_status: DeploymentStatus = DeploymentStatus.QUEUED,
    **build: Any,
) -> Job:
    async with session_factory.begin() as session:
        service = await seed_service(session, owner_github_id)
        request = await add(
            session,
            DeploymentRequest(
                service_id=service.id,
                environment=Environment.PROD,
                source_sha="a" * 40,
                trigger_type=DeploymentTrigger.MANUAL,
                idempotency_key=uuid4().hex,
                status=request_status,
            ),
        )
        new_build = await add(session, Build(deployment_request_id=request.id, **build))
        job = await add(
            session,
            Job(
                deployment_request_id=request.id,
                kind=JobKind.BUILD,
                payload={"build_id": new_build.id},
            ),
        )
    return job


def _service(
    session_factory: async_sessionmaker[AsyncSession],
    codebuild: FakeCodeBuild,
    files: dict[str, bytes] | None = None,
    worker_id: str = "worker-1",
) -> BuildService:
    return BuildService(
        session_factory,
        FakeGitHub(files or {"Dockerfile": b"FROM scratch"}),  # type: ignore[arg-type]
        codebuild,  # type: ignore[arg-type]
        FakeEcr(),  # type: ignore[arg-type]
        FakeArtifacts(),  # type: ignore[arg-type]
        SETTINGS,
        worker_id,
    )


async def _load(
    session_factory: async_sessionmaker[AsyncSession], job: Job
) -> tuple[Build, DeploymentRequest, list[Job]]:
    async with session_factory() as session:
        build = await session.get_one(Build, job.payload["build_id"])
        request = await session.get_one(DeploymentRequest, job.deployment_request_id)
        jobs = list(await session.scalars(select(Job).order_by(Job.id)))
    return build, request, jobs


async def _claim(service: BuildService) -> Job:
    job = await service.claim_next_job()
    assert job is not None
    return job


async def test_claim_next_job_concurrent_workers_claim_once(session_factory: Any) -> None:
    await _seed(session_factory)
    services = [
        _service(session_factory, FakeCodeBuild(IN_PROGRESS), worker_id=f"w{i}") for i in range(5)
    ]

    claimed = await asyncio.gather(*(service.claim_next_job() for service in services))

    assert len([job for job in claimed if job is not None]) == 1


async def test_claim_next_job_user_limit_skips_third_build(session_factory: Any) -> None:
    for _ in range(3):
        await _seed(session_factory, owner_github_id=1)
    other_user_job = await _seed(session_factory, owner_github_id=2)
    service = _service(session_factory, FakeCodeBuild(IN_PROGRESS))

    claimed = [await service.claim_next_job() for _ in range(4)]

    assert [job.id if job else None for job in claimed][2:] == [other_user_job.id, None]


async def test_claim_next_job_expired_lease_reclaims(session_factory: Any) -> None:
    await _seed(session_factory)
    first = await _claim(_service(session_factory, FakeCodeBuild(IN_PROGRESS), worker_id="dead"))
    async with session_factory.begin() as session:
        await session.execute(text("UPDATE jobs SET locked_until = now() - interval '1 second'"))

    second = await _claim(_service(session_factory, FakeCodeBuild(IN_PROGRESS), worker_id="alive"))

    assert (second.id, second.locked_by, second.attempts) == (first.id, "alive", 2)


async def test_run_success_hands_off_to_deploy(session_factory: Any) -> None:
    await _seed(session_factory)
    codebuild = FakeCodeBuild(IN_PROGRESS, SUCCEEDED)
    service = _service(session_factory, codebuild)
    job = await _claim(service)

    await service.run(job, asyncio.Event())

    build, request, jobs = await _load(session_factory, job)
    assert codebuild.started_envs[0]["BUILDER"] == "dockerfile"
    assert codebuild.started_envs[0]["IMAGE_TAG"] == f"b-{build.id}"
    assert (build.status, build.image_digest, build.log_url) == (
        BuildStatus.SUCCEEDED,
        "sha256:" + "d" * 64,
        "https://logs.example",
    )
    assert request.status == DeploymentStatus.DEPLOYING
    assert request.source_sha == "a" * 40
    assert [(j.kind, j.status, j.payload) for j in jobs] == [
        (JobKind.BUILD, JobStatus.SUCCEEDED, {"build_id": build.id}),
        (JobKind.DEPLOY, JobStatus.QUEUED, {"build_id": build.id}),
    ]


async def test_run_job_from_deployment_request_service_builds_and_records_history(
    session_factory: Any,
) -> None:
    async with session_factory.begin() as session:
        service_row = await seed_service(session)
        request = await DeploymentRequestService(
            DeploymentRequestRepository(session),
            JobRepository(session),
            DeploymentStatusHistoryRepository(session),
            BuildRepository(session),
            ServiceVariableRepository(session),
        ).create_deployment_request(
            service_row,
            source_sha="a" * 40,
            source_commit_message="feat: x",
            trigger_type=DeploymentTrigger.MANUAL,
            idempotency_key="api-1",
        )
    assert request is not None
    service = _service(session_factory, FakeCodeBuild(SUCCEEDED))
    job = await _claim(service)

    await service.run(job, asyncio.Event())

    build, loaded_request, jobs = await _load(session_factory, job)
    assert (loaded_request.status, build.status) == (
        DeploymentStatus.DEPLOYING,
        BuildStatus.SUCCEEDED,
    )
    assert [(j.kind, j.status) for j in jobs] == [
        (JobKind.BUILD, JobStatus.SUCCEEDED),
        (JobKind.DEPLOY, JobStatus.QUEUED),
    ]
    async with session_factory() as session:
        histories = await session.scalars(
            select(DeploymentStatusHistory)
            .where(DeploymentStatusHistory.deployment_request_id == request.id)
            .order_by(DeploymentStatusHistory.id)
        )
        transitions = [(h.from_status, h.to_status) for h in histories]
    assert transitions == [
        (None, DeploymentStatus.QUEUED),
        (DeploymentStatus.QUEUED, DeploymentStatus.BUILDING),
        (DeploymentStatus.BUILDING, DeploymentStatus.DEPLOYING),
    ]


async def test_run_build_phase_failure_marks_build_failed(session_factory: Any) -> None:
    await _seed(session_factory)
    service = _service(session_factory, FakeCodeBuild(CodeBuildResult("FAILED", "BUILD", None)))
    job = await _claim(service)

    with pytest.raises(BuildFailedError) as exc_info:
        await service.run(job, asyncio.Event())
    await service.fail(job, exc_info.value)

    build, request, jobs = await _load(session_factory, job)
    assert exc_info.value.failure_code == FailureCode.BUILD_FAILED
    assert (build.status, request.status, request.failure_code) == (
        BuildStatus.FAILED,
        DeploymentStatus.FAILED,
        FailureCode.BUILD_FAILED,
    )
    assert jobs[0].status == JobStatus.FAILED


@pytest.mark.parametrize(
    "result",
    [CodeBuildResult("FAULT", None, None), CodeBuildResult("FAILED", "INSTALL", None)],
)
async def test_run_codebuild_fault_resets_and_retries(
    session_factory: Any, result: CodeBuildResult
) -> None:
    await _seed(session_factory)
    service = _service(session_factory, FakeCodeBuild(result))
    job = await _claim(service)

    with pytest.raises(ExternalError) as exc_info:
        await service.run(job, asyncio.Event())
    await service.retry_or_fail(job, exc_info.value)

    build, _, jobs = await _load(session_factory, job)
    assert (build.codebuild_build_id, build.attempt) == (None, 2)
    assert jobs[0].status == JobStatus.RETRY_WAIT


async def test_run_recorded_codebuild_id_resumes_without_start(session_factory: Any) -> None:
    await _seed(
        session_factory,
        request_status=DeploymentStatus.BUILDING,
        status=BuildStatus.BUILDING,
        codebuild_build_id="iris-build:existing",
        image_repository=REPOSITORY_URI,
        image_tag="b-1",
    )
    codebuild = FakeCodeBuild(SUCCEEDED)
    service = _service(session_factory, codebuild)
    job = await _claim(service)

    await service.run(job, asyncio.Event())

    build, _, _ = await _load(session_factory, job)
    assert codebuild.started_envs == []
    assert build.status == BuildStatus.SUCCEEDED


async def test_run_cancel_requested_stops_codebuild(session_factory: Any) -> None:
    await _seed(session_factory)
    codebuild = FakeCodeBuild(IN_PROGRESS)
    service = _service(session_factory, codebuild)
    job = await _claim(service)
    task = asyncio.create_task(service.run(job, asyncio.Event()))
    await asyncio.wait_for(codebuild.started.wait(), timeout=5)
    async with session_factory.begin() as session:
        await session.execute(text("UPDATE deployment_requests SET cancel_requested_at = now()"))

    await asyncio.wait_for(task, timeout=5)

    build, request, jobs = await _load(session_factory, job)
    assert codebuild.stopped == ["iris-build:1"]
    assert (build.status, request.status, jobs[0].status) == (
        BuildStatus.CANCELLED,
        DeploymentStatus.SUPERSEDED,
        JobStatus.SUCCEEDED,
    )


async def test_run_stop_signal_releases_job(session_factory: Any) -> None:
    await _seed(session_factory)
    service = _service(session_factory, FakeCodeBuild(IN_PROGRESS))
    job = await _claim(service)
    stop = asyncio.Event()
    stop.set()

    await service.run(job, stop)

    build, _, jobs = await _load(session_factory, job)
    assert build.status == BuildStatus.BUILDING
    assert (jobs[0].status, jobs[0].attempts, jobs[0].locked_by) == (JobStatus.QUEUED, 0, None)
