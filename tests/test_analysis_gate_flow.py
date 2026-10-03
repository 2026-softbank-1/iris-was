"""레포 구성 분석(Analysis Gate) 통합 테스트. TEST_DATABASE_URL 의 로컬 PostgreSQL 이 필요하다.

선점(SKIP LOCKED·lease 회수)·NOTIFY 트리거·Build Worker 분석 루프를 실제 DB 로 보고, 분석기는
이미지에 설치된 iris-analyzer gate CLI 를 실제 subprocess 로 실행한다(GitHub 만 가짜다).
접수(API 서비스) → Worker 실행 → apply → 배포 요청(BUILD job)까지 한 번에 확인한다.
"""

import asyncio
import io
import os
import sys
import tarfile
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.analysis_gate_client import SubprocessAnalysisGateClient
from app.clients.source_repository_client import BranchInfo, CommitInfo
from app.core.config import BuildWorkerSettings
from app.core.exceptions import ArchiveInvalidError, ArchiveTooLargeError, NotFoundError
from app.enums import (
    AnalysisErrorCode,
    AnalysisGateMode,
    Builder,
    JobKind,
    RepositoryAnalysisStatus,
)
from app.models import (
    DeploymentRequest,
    GithubInstallation,
    Job,
    Project,
    RepositoryAnalysis,
    Service,
    User,
    UserGithubInstallation,
)
from app.repositories.build_repository import BuildRepository
from app.repositories.deployment_request_repository import DeploymentRequestRepository
from app.repositories.deployment_status_history_repository import (
    DeploymentStatusHistoryRepository,
)
from app.repositories.github_installation_repository import GithubInstallationRepository
from app.repositories.job_repository import JobRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.repository_analysis_repository import RepositoryAnalysisRepository
from app.repositories.service_repository import ServiceRepository
from app.repositories.service_upload_repository import ServiceUploadRepository
from app.repositories.service_variable_repository import ServiceVariableRepository
from app.repositories.target_repository import TargetRepository
from app.services.analysis_gate_service import (
    MAX_ATTEMPTS,
    AnalysisGateService,
    extract_source_snapshot,
)
from app.services.deployment_request_service import DeploymentRequestService
from app.services.manual_deployment_service import ManualDeploymentService
from app.services.repository_analysis_service import RepositoryAnalysisService, UnitSelection
from app.services.service_registry_service import ServiceRegistryService
from app.services.source_archive import ArchiveLimits
from app.services.source_repository_service import SourceRepositoryService
from app.workers.build_worker import run_analyses
from tests.fakes import FakeSourceRepositoryClient, make_repository
from tests.fakes_project import FakeTeardownService
from tests.worker_support import (
    TEST_DATABASE_URL,
    add,
    requires_database,
    session_factory_with_clean_data,
)

pytestmark = [pytest.mark.integration, requires_database]

SHA = "e" * 40
REPOSITORY_URL = "https://github.com/owner/shop"
SETTINGS = BuildWorkerSettings(
    github_app_id=1,
    github_app_private_key="unused",
    aws_region="ap-northeast-2",
    codebuild_project="iris-test-build",
    artifact_bucket="iris-test-artifacts",
    analysis_gate_timeout_seconds=60,
)

COMPOSE_FILES = {
    "compose.yaml": b"""services:
  web:
    build: ./web
    ports: ["3000:3000"]
    depends_on: [api]
  api:
    build: ./api
    ports: ["8000:8000"]
    environment:
      DATABASE_URL: postgres://shop:shop@postgres:5432/shop
      REDIS_URL: redis://redis:6379
    depends_on: [postgres, redis]
  worker:
    build: ./worker
    environment:
      REDIS_URL: redis://redis:6379
    depends_on: [redis]
  postgres:
    image: postgres:16-alpine
  redis:
    image: redis:7-alpine
""",
    "web/Dockerfile": b"FROM node:20-alpine\nWORKDIR /app\nCOPY . .\nEXPOSE 3000\n"
    b'CMD ["npm", "start"]\n',
    "web/package.json": b'{"name": "web", "scripts": {"start": "node server.js"}}\n',
    "api/Dockerfile": b"FROM node:20-alpine\nWORKDIR /app\nCOPY . .\nEXPOSE 8000\n"
    b'CMD ["node", "index.js"]\n',
    "api/package.json": b'{"name": "api", "scripts": {"start": "node index.js"}}\n',
    "worker/Dockerfile": b'FROM python:3.12-slim\nCOPY . .\nCMD ["python", "worker.py"]\n',
    "worker/requirements.txt": b"redis==5.0.0\n",
}
SINGLE_DOCKERFILE_FILES = {
    "Dockerfile": b'FROM node:20-alpine\nCOPY . .\nEXPOSE 8080\nCMD ["node", "index.js"]\n',
    "package.json": b'{"name": "app"}\n',
    "index.js": b"require('http').createServer().listen(8080)\n",
}


class FakeGitHub:
    """GitHub tarball 처럼 `{owner}-{repo}-{sha}/` 아래에 files 를 담는다."""

    def __init__(self, files: dict[str, bytes], error: Exception | None = None) -> None:
        self.files = files
        self.error = error
        self.downloads: list[str] = []

    async def create_installation_token(
        self, installation_id: int, repository_name: str | None
    ) -> str:
        return "token"

    async def get_branch_sha(self, token: str, full_name: str, branch: str | None = None) -> str:
        return SHA

    async def download_tarball(
        self, token: str, full_name: str, sha: str, dest: Path, max_bytes: int
    ) -> None:
        self.downloads.append(sha)
        if self.error is not None:
            raise self.error
        _write_tarball(dest, self.files, f"owner-shop-{sha[:7]}")


def _write_tarball(dest: Path, files: dict[str, bytes], root: str) -> None:
    with tarfile.open(dest, "w:gz") as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(f"{root}/{name}")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async for factory in session_factory_with_clean_data():
        yield factory


async def _seed_owner(session: AsyncSession) -> tuple[User, Project, GithubInstallation]:
    user = await add(session, User(github_id=1, login="owner"))
    installation = await add(
        session,
        GithubInstallation(installation_id=700001, account_login="owner", account_type="User"),
    )
    await add(
        session, UserGithubInstallation(user_id=user.id, github_installation_id=installation.id)
    )
    project = await add(session, Project(name="shop", owner_id=user.id))
    return user, project, installation


async def _seed_analysis(
    session_factory: async_sessionmaker[AsyncSession], **overrides: Any
) -> RepositoryAnalysis:
    async with session_factory.begin() as session:
        user, project, installation = await _seed_owner(session)
        values: dict[str, Any] = {
            "project_id": project.id,
            "user_id": user.id,
            "source_repository_url": REPOSITORY_URL,
            "github_installation_id": installation.id,
            "source_branch": "main",
            "source_sha": SHA,
            "mode": AnalysisGateMode.AUTO,
            **overrides,
        }
        return await add(session, RepositoryAnalysis(**values))


def _service(
    session_factory: async_sessionmaker[AsyncSession],
    github: FakeGitHub,
    command: list[str] | None = None,
    worker_id: str = "worker-1",
    timeout_seconds: float = 60,
) -> AnalysisGateService:
    return AnalysisGateService(
        session_factory=session_factory,
        github=github,  # type: ignore[arg-type]
        analyzer=SubprocessAnalysisGateClient(
            command or SETTINGS.analysis_gate_command, timeout_seconds=timeout_seconds
        ),
        settings=SETTINGS,
        worker_id=worker_id,
    )


async def _load(
    session_factory: async_sessionmaker[AsyncSession], analysis_id: int
) -> RepositoryAnalysis:
    async with session_factory() as session:
        return await session.get_one(RepositoryAnalysis, analysis_id)


async def _claim_and_run(service: AnalysisGateService) -> RepositoryAnalysis:
    analysis = await service.claim_next_analysis()
    assert analysis is not None
    await service.run(analysis)
    return analysis


def _fake_command(tmp_path: Path, code: str) -> list[str]:
    script = tmp_path / "fake_analyzer.py"
    script.write_text(code)
    return [sys.executable, str(script)]


async def test_claim_skips_locked_rows_and_reclaims_expired_lease(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first = await _seed_analysis(session_factory)
    async with session_factory.begin() as session:
        second = await add(
            session,
            RepositoryAnalysis(
                project_id=first.project_id,
                user_id=first.user_id,
                source_repository_url=REPOSITORY_URL,
                source_branch="main",
                mode=AnalysisGateMode.AUTO,
            ),
        )
    worker_a = _service(session_factory, FakeGitHub({}), worker_id="a")
    worker_b = _service(session_factory, FakeGitHub({}), worker_id="b")

    claimed_a = await worker_a.claim_next_analysis()
    claimed_b = await worker_b.claim_next_analysis()

    assert claimed_a is not None and claimed_b is not None
    assert (claimed_a.id, claimed_b.id) == (first.id, second.id)
    assert claimed_a.status == RepositoryAnalysisStatus.RUNNING
    assert claimed_a.locked_by == "a" and claimed_a.attempts == 1
    assert await worker_b.claim_next_analysis() is None

    # a 가 죽어 lease 가 만료되면 b 가 다시 가져간다.
    async with session_factory.begin() as session:
        await session.execute(
            update(RepositoryAnalysis)
            .where(RepositoryAnalysis.id == first.id)
            .values(locked_until=func.now() - timedelta(seconds=1))
        )
    reclaimed = await worker_b.claim_next_analysis()
    assert reclaimed is not None
    assert (reclaimed.id, reclaimed.locked_by, reclaimed.attempts) == (first.id, "b", 2)


async def test_concurrent_claims_never_share_an_analysis(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first = await _seed_analysis(session_factory)
    async with session_factory.begin() as session:
        for _ in range(4):
            await add(
                session,
                RepositoryAnalysis(
                    project_id=first.project_id,
                    user_id=first.user_id,
                    source_repository_url=REPOSITORY_URL,
                    source_branch="main",
                    mode=AnalysisGateMode.AUTO,
                ),
            )
    workers = [_service(session_factory, FakeGitHub({}), worker_id=f"w{i}") for i in range(8)]

    claimed = await asyncio.gather(*(w.claim_next_analysis() for w in workers))

    ids = [a.id for a in claimed if a is not None]
    assert len(ids) == 5 and len(set(ids)) == 5


async def test_insert_and_release_notify_workers(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    url = make_url(TEST_DATABASE_URL or "")
    connection = await asyncpg.connect(
        user=url.username,
        password=url.password,
        host=url.host,
        port=url.port,
        database=url.database,
    )
    payloads: asyncio.Queue[str] = asyncio.Queue()
    await connection.add_listener("jobs", lambda *args: payloads.put_nowait(args[3]))
    try:
        analysis = await _seed_analysis(session_factory)
        assert await asyncio.wait_for(payloads.get(), 5) == "REPOSITORY_ANALYSIS"

        service = _service(session_factory, FakeGitHub({}))
        claimed = await service.claim_next_analysis()
        assert claimed is not None
        await service.release(analysis.id)
        assert await asyncio.wait_for(payloads.get(), 5) == "REPOSITORY_ANALYSIS"
    finally:
        await connection.close()
    released = await _load(session_factory, analysis.id)
    assert released.status == RepositoryAnalysisStatus.QUEUED
    assert (released.attempts, released.locked_by) == (0, None)


async def test_run_real_analyzer_on_multi_image_compose_repository_analyzes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    analysis = await _seed_analysis(session_factory)
    github = FakeGitHub(COMPOSE_FILES)

    await _claim_and_run(_service(session_factory, github))

    stored = await _load(session_factory, analysis.id)
    assert stored.status == RepositoryAnalysisStatus.SUCCEEDED, stored.error_message
    assert (stored.decision, stored.complexity) == ("analyze", "complex")
    assert stored.locked_until is None
    assert github.downloads == [SHA]
    result = stored.result
    assert result is not None
    assert result["schemaVersion"] == "iris.analysis-gate.v1"
    assert result["sourceSha"] == SHA
    assert result["executionAuthorized"] is False
    assert {reason["code"] for reason in result["reasons"]} >= {"compose_multi_build"}
    units = {unit["rootDirectory"]: unit for unit in result["units"]}
    assert set(units) == {"web", "api", "worker"}
    assert all(unit["builder"] == "dockerfile" for unit in units.values())
    assert {dependency["engine"] for dependency in result["dependencies"]} >= {"postgres", "redis"}


async def test_run_real_analyzer_on_single_dockerfile_repository_skips(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    analysis = await _seed_analysis(session_factory)

    await _claim_and_run(_service(session_factory, FakeGitHub(SINGLE_DOCKERFILE_FILES)))

    stored = await _load(session_factory, analysis.id)
    assert stored.status == RepositoryAnalysisStatus.SUCCEEDED, stored.error_message
    assert (stored.decision, stored.complexity) == ("skip", "simple")
    assert stored.result is not None
    assert stored.result["simpleBuild"] == {"builder": "dockerfile", "dockerfilePath": "Dockerfile"}
    assert stored.result["units"] == []


async def test_run_real_analyzer_force_mode_analyzes_simple_repository(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    analysis = await _seed_analysis(session_factory, mode=AnalysisGateMode.FORCE)

    await _claim_and_run(_service(session_factory, FakeGitHub(SINGLE_DOCKERFILE_FILES)))

    stored = await _load(session_factory, analysis.id)
    assert stored.status == RepositoryAnalysisStatus.SUCCEEDED, stored.error_message
    assert stored.decision == "analyze"
    assert stored.result is not None
    assert "forced" in {reason["code"] for reason in stored.result["reasons"]}


async def test_run_real_analyzer_respects_root_directory(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    analysis = await _seed_analysis(session_factory, root_directory="api")

    await _claim_and_run(_service(session_factory, FakeGitHub(COMPOSE_FILES)))

    stored = await _load(session_factory, analysis.id)
    assert stored.status == RepositoryAnalysisStatus.SUCCEEDED, stored.error_message
    assert stored.decision == "skip"
    assert stored.result is not None
    assert stored.result["rootDirectory"] == "api"


async def test_run_resolves_branch_head_when_sha_missing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    analysis = await _seed_analysis(session_factory, source_sha=None)

    await _claim_and_run(_service(session_factory, FakeGitHub(SINGLE_DOCKERFILE_FILES)))

    stored = await _load(session_factory, analysis.id)
    assert stored.source_sha == SHA
    assert stored.status == RepositoryAnalysisStatus.SUCCEEDED


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("import sys\nsys.exit(2)\n", AnalysisErrorCode.ANALYZER_FAILED),
        ("print('not json')\n", AnalysisErrorCode.ANALYZER_FAILED),
        ("import time\ntime.sleep(10)\n", AnalysisErrorCode.ANALYZER_TIMED_OUT),
    ],
    ids=["nonzero", "bad-json", "timeout"],
)
async def test_run_records_analyzer_failure(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    code: str,
    expected: AnalysisErrorCode,
) -> None:
    analysis = await _seed_analysis(session_factory)
    service = _service(
        session_factory,
        FakeGitHub(SINGLE_DOCKERFILE_FILES),
        command=_fake_command(tmp_path, code),
        timeout_seconds=0.5,
    )

    await _claim_and_run(service)

    stored = await _load(session_factory, analysis.id)
    assert (stored.status, stored.error_code) == (RepositoryAnalysisStatus.FAILED, expected)
    assert stored.locked_until is None


async def test_run_records_missing_analyzer_as_unavailable(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    analysis = await _seed_analysis(session_factory)

    await _claim_and_run(
        _service(session_factory, FakeGitHub({}), command=[str(tmp_path / "missing")])
    )

    stored = await _load(session_factory, analysis.id)
    assert stored.error_code == AnalysisErrorCode.ANALYZER_UNAVAILABLE


async def test_run_records_missing_commit_as_source_ref_not_found(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    analysis = await _seed_analysis(session_factory)

    await _claim_and_run(_service(session_factory, FakeGitHub({}, error=NotFoundError())))

    stored = await _load(session_factory, analysis.id)
    assert stored.error_code == AnalysisErrorCode.SOURCE_REF_NOT_FOUND


async def test_run_without_installation_is_not_accessible(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    analysis = await _seed_analysis(session_factory, github_installation_id=None)

    await _claim_and_run(_service(session_factory, FakeGitHub(SINGLE_DOCKERFILE_FILES)))

    stored = await _load(session_factory, analysis.id)
    assert stored.error_code == AnalysisErrorCode.SOURCE_NOT_ACCESSIBLE


async def test_run_after_too_many_attempts_fails_as_interrupted(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    analysis = await _seed_analysis(session_factory, attempts=MAX_ATTEMPTS)
    github = FakeGitHub(SINGLE_DOCKERFILE_FILES)

    await _claim_and_run(_service(session_factory, github))

    stored = await _load(session_factory, analysis.id)
    assert stored.error_code == AnalysisErrorCode.ANALYSIS_INTERRUPTED
    assert github.downloads == []


async def test_run_after_lease_lost_keeps_new_owners_result(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    analysis = await _seed_analysis(session_factory)
    service = _service(session_factory, FakeGitHub(SINGLE_DOCKERFILE_FILES), worker_id="old")
    claimed = await service.claim_next_analysis()
    assert claimed is not None
    async with session_factory.begin() as session:
        await session.execute(
            update(RepositoryAnalysis)
            .where(RepositoryAnalysis.id == analysis.id)
            .values(locked_by="new")
        )

    await service.run(claimed)

    stored = await _load(session_factory, analysis.id)
    assert (stored.status, stored.locked_by, stored.result) == (
        RepositoryAnalysisStatus.RUNNING,
        "new",
        None,
    )


class _ImmediateWakeup:
    def clear(self) -> None:
        pass

    def retry_soon(self) -> None:
        pass

    async def wait(self, stop: asyncio.Event) -> None:
        await asyncio.sleep(0.05)


async def test_worker_loop_runs_analysis_and_stops(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    analysis = await _seed_analysis(session_factory)
    stop = asyncio.Event()
    service = _service(session_factory, FakeGitHub(COMPOSE_FILES))
    loop = asyncio.create_task(run_analyses(stop, service, 2, _ImmediateWakeup()))  # type: ignore[arg-type]

    for _ in range(600):
        if (await _load(session_factory, analysis.id)).status == "SUCCEEDED":
            break
        await asyncio.sleep(0.1)
    else:
        pytest.fail("analysis did not finish")
    stop.set()
    await asyncio.wait_for(loop, 5)


async def test_worker_stop_kills_analyzer_and_requeues_analysis(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    analysis = await _seed_analysis(session_factory)
    marker = tmp_path / "pid"
    command = _fake_command(
        tmp_path,
        f"import os, pathlib, time\npathlib.Path({str(marker)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(60)\n",
    )
    stop = asyncio.Event()
    service = _service(session_factory, FakeGitHub(SINGLE_DOCKERFILE_FILES), command=command)
    loop = asyncio.create_task(run_analyses(stop, service, 1, _ImmediateWakeup()))  # type: ignore[arg-type]
    for _ in range(200):
        if marker.exists():
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("analyzer did not start")

    stop.set()
    await asyncio.wait_for(loop, 10)

    stored = await _load(session_factory, analysis.id)
    assert (stored.status, stored.attempts, stored.locked_by) == (
        RepositoryAnalysisStatus.QUEUED,
        0,
        None,
    )
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)


def test_extract_source_snapshot_drops_links_and_strips_root(tmp_path: Path) -> None:
    archive_path = tmp_path / "source.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        for name, content in {"repo-sha/app/main.py": b"print(1)\n"}.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        link = tarfile.TarInfo("repo-sha/secret")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        archive.addfile(link)
    target = tmp_path / "out"

    extract_source_snapshot(archive_path, target, ArchiveLimits(10_000, 100))

    assert (target / "app/main.py").read_text() == "print(1)\n"
    assert not (target / "secret").exists()


def test_extract_source_snapshot_rejects_traversal(tmp_path: Path) -> None:
    archive_path = tmp_path / "source.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        info = tarfile.TarInfo("repo-sha/../../escape.txt")
        info.size = 1
        archive.addfile(info, io.BytesIO(b"x"))

    with pytest.raises(ArchiveInvalidError):
        extract_source_snapshot(archive_path, tmp_path / "out", ArchiveLimits(10_000, 100))
    assert not (tmp_path / "escape.txt").exists()


def test_extract_source_snapshot_enforces_limits(tmp_path: Path) -> None:
    archive_path = tmp_path / "source.tar.gz"
    _write_tarball(archive_path, {"a.txt": b"x" * 100, "b.txt": b"y" * 100}, "repo-sha")

    with pytest.raises(ArchiveTooLargeError):
        extract_source_snapshot(archive_path, tmp_path / "bytes", ArchiveLimits(150, 100))
    with pytest.raises(ArchiveTooLargeError):
        extract_source_snapshot(archive_path, tmp_path / "entries", ArchiveLimits(10_000, 1))


def test_extract_source_snapshot_rejects_corrupt_archive(tmp_path: Path) -> None:
    archive_path = tmp_path / "source.tar.gz"
    archive_path.write_bytes(b"not a tarball")

    with pytest.raises(ArchiveInvalidError):
        extract_source_snapshot(archive_path, tmp_path / "out", ArchiveLimits(10_000, 100))


async def test_api_worker_apply_creates_services_and_build_jobs(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """접수(API 서비스) → Build Worker 분석(실제 분석기) → apply → 배포 요청·BUILD job."""
    async with session_factory.begin() as session:
        user, _, _ = await _seed_owner(session)
        owner_id = user.id
        project_id = (await session.scalars(select(Project.id))).one()
    github_api = FakeSourceRepositoryClient({700001: [make_repository("owner/shop")]})
    github_api.branches["owner/shop"] = [BranchInfo("main", True)]
    github_api.heads[("owner/shop", "main")] = CommitInfo(SHA, "init")

    async with session_factory() as session:
        created = await _analysis_api(session, github_api).create_analysis(
            owner_id, project_id, REPOSITORY_URL, None, None, AnalysisGateMode.AUTO
        )
    assert created.source_sha == SHA

    await _claim_and_run(_service(session_factory, FakeGitHub(COMPOSE_FILES)))

    stored = await _load(session_factory, created.id)
    assert stored.status == RepositoryAnalysisStatus.SUCCEEDED, stored.error_message
    assert stored.result is not None
    unit_ids = [unit["id"] for unit in stored.result["units"]]
    async with session_factory() as session:
        applied = await _analysis_api(session, github_api).apply_analysis(
            owner_id,
            project_id,
            created.id,
            [UnitSelection(unit_id=unit_id) for unit_id in unit_ids],
            should_deploy=True,
        )
    async with session_factory() as session:
        again = await _analysis_api(session, github_api).apply_analysis(
            owner_id, project_id, created.id, [], should_deploy=True
        )

    assert [d.service.id for d in again.services] == [d.service.id for d in applied.services]
    async with session_factory() as session:
        services = list((await session.scalars(select(Service).order_by(Service.id))).all())
        requests = list((await session.scalars(select(DeploymentRequest))).all())
        jobs = list((await session.scalars(select(Job))).all())
        analysis = await session.get_one(RepositoryAnalysis, created.id)
    assert {s.root_directory for s in services} == {"web", "api", "worker"}
    assert all(s.builder == Builder.DOCKERFILE for s in services)
    assert all(s.dockerfile_path == "Dockerfile" for s in services)
    assert all(
        s.analysis_plan and s.analysis_plan["gate"]["analysisId"] == created.id for s in services
    )
    assert analysis.status == RepositoryAnalysisStatus.APPLIED
    assert analysis.applied_service_ids == [s.id for s in services]
    # 서비스마다 분석한 커밋으로 배포 요청 하나와 BUILD job 하나. 다시 apply 해도 늘지 않는다.
    assert sorted(r.service_id for r in requests) == [s.id for s in services]
    assert {r.source_sha for r in requests} == {SHA}
    assert [j.kind for j in jobs] == [JobKind.BUILD] * len(services)


def _analysis_api(
    session: AsyncSession, github_api: FakeSourceRepositoryClient
) -> RepositoryAnalysisService:
    source = SourceRepositoryService(GithubInstallationRepository(session), github_api)  # type: ignore[arg-type]
    deployment_requests = DeploymentRequestService(
        DeploymentRequestRepository(session),
        JobRepository(session),
        DeploymentStatusHistoryRepository(session),
        BuildRepository(session),
        ServiceVariableRepository(session),
        ServiceRepository(session),
    )
    registry = ServiceRegistryService(
        session,
        ProjectRepository(session),
        ServiceRepository(session),
        TargetRepository(session),
        GithubInstallationRepository(session),
        source,
        DeploymentRequestRepository(session),
        FakeTeardownService(),  # type: ignore[arg-type]
        repository_analysis_repository=RepositoryAnalysisRepository(session),
    )
    manual = ManualDeploymentService(
        session,
        ServiceRepository(session),
        DeploymentRequestRepository(session),
        BuildRepository(session),
        deployment_requests,
        source,
        ServiceUploadRepository(session),
    )
    return RepositoryAnalysisService(
        session,
        ProjectRepository(session),
        RepositoryAnalysisRepository(session),
        GithubInstallationRepository(session),
        source,
        registry,
        manual,
    )
