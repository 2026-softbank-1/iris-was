"""CLI 업로드를 소스로 쓰는 BUILD job 통합 테스트. TEST_DATABASE_URL 의 로컬 PostgreSQL 이 필요하다.

GitHub 대신 올린 아카이브를 받아 검사·재패킹하고, 그 뒤 CodeBuild·배포 단계는 GitHub 요청과 같다는
점, 위험한 아카이브는 CodeBuild 를 시작하기 전에 소스 문제로 끝난다는 점을 본다.
"""

import asyncio
import hashlib
import io
import tarfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
from app.models import Build, DeploymentRequest, Job, Project, ServiceUpload
from app.services.build_service import BuildService
from tests.fakes_upload import FakeUploadStorage
from tests.test_build_service import (
    SETTINGS,
    SUCCEEDED,
    FakeArtifacts,
    FakeCodeBuild,
    FakeGitHub,
    _claim,
    _load,
    _seed,
    _service,
)
from tests.worker_support import (
    add,
    requires_database,
    seed_service,
    session_factory_with_clean_data,
)

pytestmark = [pytest.mark.integration, requires_database]

STORAGE_KEY = "uploads/up-1.tar.gz"


class CountingGitHub(FakeGitHub):
    """CLI 빌드는 GitHub 를 부르지 않는다는 점을 확인하려고 호출을 센다."""

    def __init__(self) -> None:
        super().__init__({"Dockerfile": b"FROM scratch"})
        self.calls = 0

    async def create_installation_token(
        self, installation_id: int, repository_name: str | None
    ) -> str:
        self.calls += 1
        return await super().create_installation_token(installation_id, repository_name)

    async def download_tarball(
        self, token: str, full_name: str, sha: str, dest: Path, max_bytes: int
    ) -> None:
        self.calls += 1
        await super().download_tarball(token, full_name, sha, dest, max_bytes)


def settings_with(**overrides: Any) -> BuildWorkerSettings:
    return BuildWorkerSettings(**{**SETTINGS.model_dump(), **overrides})


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async for factory in session_factory_with_clean_data():
        yield factory


def _targz(members: list[tuple[str, bytes | None, bytes]]) -> bytes:
    """(이름, 내용, 타입) 목록으로 tar.gz 를 만든다. 내용이 None 이면 데이터 없는 항목이다."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content, type_ in members:
            info = tarfile.TarInfo(name)
            info.type = type_
            if type_ in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                info.linkname = (content or b"").decode()
            elif content is not None:
                info.size = len(content)
            archive.addfile(info, io.BytesIO(content) if info.size else None)
    return buffer.getvalue()


def _source(files: dict[str, bytes]) -> bytes:
    return _targz([(name, content, tarfile.REGTYPE) for name, content in files.items()])


async def _seed_cli_build(
    session_factory: async_sessionmaker[AsyncSession],
    archive: bytes,
    *,
    root_directory: str | None = None,
    recorded_sha256: str | None = None,
    recorded_size: int | None = None,
) -> Job:
    async with session_factory.begin() as session:
        service = await seed_service(session)
        service.root_directory = root_directory
        upload = await add(
            session,
            ServiceUpload(
                public_id="up-1",
                service_id=service.id,
                uploaded_by=(await session.get_one(Project, service.project_id)).owner_id,
                size_bytes=recorded_size if recorded_size is not None else len(archive),
                sha256=recorded_sha256 or hashlib.sha256(archive).hexdigest(),
                storage_key=STORAGE_KEY,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                consumed_at=datetime.now(UTC),
            ),
        )
        request = await add(
            session,
            DeploymentRequest(
                service_id=service.id,
                environment=Environment.PROD,
                source_sha="upload-" + upload.sha256[:12],
                trigger_type=DeploymentTrigger.CLI,
                idempotency_key=uuid4().hex,
                service_upload_id=upload.id,
            ),
        )
        build = await add(session, Build(deployment_request_id=request.id))
        return await add(
            session,
            Job(
                deployment_request_id=request.id,
                kind=JobKind.BUILD,
                payload={"build_id": build.id},
            ),
        )


def _storage(archive: bytes) -> FakeUploadStorage:
    storage = FakeUploadStorage()
    storage.objects[STORAGE_KEY] = archive
    return storage


def _worker(
    session_factory: Any,
    codebuild: FakeCodeBuild,
    storage: FakeUploadStorage,
    *,
    github: CountingGitHub | None = None,
    artifacts: FakeArtifacts | None = None,
    settings: BuildWorkerSettings | None = None,
) -> BuildService:
    return _service(
        session_factory,
        codebuild,
        github=github or CountingGitHub(),
        artifacts=artifacts,
        uploads=storage,
        settings=settings or settings_with(),
    )


async def test_run_cli_upload_builds_from_repacked_snapshot_without_github(
    session_factory: Any,
) -> None:
    archive = _source({"Dockerfile": b"FROM scratch", "src/app.py": b"print(1)"})
    job = await _seed_cli_build(session_factory, archive)
    codebuild, github, artifacts = FakeCodeBuild(SUCCEEDED), CountingGitHub(), FakeArtifacts()
    service = _worker(
        session_factory, codebuild, _storage(archive), github=github, artifacts=artifacts
    )
    job = await _claim(service)

    await service.run(job, asyncio.Event())

    build, request, jobs = await _load(session_factory, job)
    env = codebuild.started_envs[0]
    assert github.calls == 0
    assert (env["BUILDER"], env["ROOT_DIRECTORY"], env["DOCKERFILE_PATH"]) == (
        "dockerfile",
        ".",
        "Dockerfile",
    )
    assert env["SOURCE_URL"] == f"https://s3.example/snapshots/{build.id}.tar.gz"
    # 스냅샷은 GitHub tarball 처럼 최상위 디렉터리 아래에 있다(buildspec 이 그 한 단계를 벗긴다).
    with tarfile.open(fileobj=io.BytesIO(artifacts.snapshots[build.id]), mode="r:gz") as snapshot:
        assert sorted(m.name for m in snapshot.getmembers() if m.isfile()) == [
            "source/Dockerfile",
            "source/src/app.py",
        ]
    assert request.source_sha == build.source_sha
    assert build.source_sha is not None and build.source_sha.startswith("upload-")
    # 이후 단계는 GitHub 요청과 같다: 빌드 성공 → DEPLOY job.
    assert (build.status, request.status) == (BuildStatus.SUCCEEDED, DeploymentStatus.DEPLOYING)
    assert [(j.kind, j.status) for j in jobs] == [
        (JobKind.BUILD, JobStatus.SUCCEEDED),
        (JobKind.DEPLOY, JobStatus.QUEUED),
    ]


async def test_run_cli_upload_ignores_service_root_directory(session_factory: Any) -> None:
    archive = _source({"Dockerfile": b"FROM scratch"})
    job = await _seed_cli_build(session_factory, archive, root_directory="apps/web")
    codebuild = FakeCodeBuild(SUCCEEDED)
    service = _worker(session_factory, codebuild, _storage(archive))
    job = await _claim(service)

    await service.run(job, asyncio.Event())

    assert codebuild.started_envs[0]["ROOT_DIRECTORY"] == "."
    assert codebuild.started_envs[0]["BUILDER"] == "dockerfile"


async def test_run_cli_upload_reads_iris_json_at_archive_root(session_factory: Any) -> None:
    config = b'{"deploy": {"healthcheckPath": "/health"}}'
    archive = _source({"Dockerfile": b"FROM scratch", "iris.json": config})
    job = await _seed_cli_build(session_factory, archive)
    service = _worker(session_factory, FakeCodeBuild(SUCCEEDED), _storage(archive))
    job = await _claim(service)

    await service.run(job, asyncio.Event())

    build, _, _ = await _load(session_factory, job)
    assert build.deploy_config == {"healthcheckPath": "/health"}


async def test_run_cli_upload_detects_dockerfile_that_is_a_hardlink(session_factory: Any) -> None:
    archive = _targz(
        [
            ("build/Dockerfile.prod", b"FROM scratch", tarfile.REGTYPE),
            ("Dockerfile", b"build/Dockerfile.prod", tarfile.LNKTYPE),
        ]
    )
    job = await _seed_cli_build(session_factory, archive)
    codebuild = FakeCodeBuild(SUCCEEDED)
    service = _worker(session_factory, codebuild, _storage(archive))
    job = await _claim(service)

    await service.run(job, asyncio.Event())

    assert codebuild.started_envs[0]["BUILDER"] == "dockerfile"


async def test_run_github_source_does_not_read_uploads(session_factory: Any) -> None:
    job = await _seed(session_factory)
    storage = FakeUploadStorage()
    github, codebuild = CountingGitHub(), FakeCodeBuild(SUCCEEDED)
    service = _worker(session_factory, codebuild, storage, github=github)
    job = await _claim(service)

    await service.run(job, asyncio.Event())

    build, _, _ = await _load(session_factory, job)
    assert (github.calls, storage.downloads) == (2, [])
    assert codebuild.started_envs[0]["ROOT_DIRECTORY"] == "."
    assert build.status == BuildStatus.SUCCEEDED


@pytest.mark.parametrize(
    "members",
    [
        pytest.param([("../outside.txt", b"x", tarfile.REGTYPE)], id="parent-traversal"),
        pytest.param([("/etc/cron.d/evil", b"x", tarfile.REGTYPE)], id="absolute-path"),
        pytest.param([("link", b"/etc", tarfile.SYMTYPE)], id="absolute-symlink"),
        pytest.param(
            [("d/s", b"..", tarfile.SYMTYPE), ("t", b"d/s/..", tarfile.SYMTYPE)],
            id="symlink-chain-escape",
        ),
        pytest.param(
            [("link", b"somewhere", tarfile.SYMTYPE), ("link/evil", b"x", tarfile.REGTYPE)],
            id="write-through-symlink",
        ),
        pytest.param([("dev/null", None, tarfile.CHRTYPE)], id="device-file"),
    ],
)
async def test_run_cli_upload_with_hostile_archive_fails_as_invalid_source(
    session_factory: Any, members: list[tuple[str, bytes | None, bytes]]
) -> None:
    archive = _targz([("Dockerfile", b"FROM scratch", tarfile.REGTYPE), *members])
    job = await _seed_cli_build(session_factory, archive)
    codebuild, artifacts = FakeCodeBuild(SUCCEEDED), FakeArtifacts()
    service = _worker(session_factory, codebuild, _storage(archive), artifacts=artifacts)
    job = await _claim(service)

    with pytest.raises(BuildFailedError) as raised:
        await service.run(job, asyncio.Event())
    await service.fail(job, raised.value)

    build, request, jobs = await _load(session_factory, job)
    assert raised.value.failure_code == FailureCode.SOURCE_INVALID
    assert (codebuild.started_envs, artifacts.snapshots) == ([], {})
    assert (build.status, build.failure_code) == (BuildStatus.FAILED, FailureCode.SOURCE_INVALID)
    assert (request.status, request.failure_code) == (
        DeploymentStatus.FAILED,
        FailureCode.SOURCE_INVALID,
    )
    assert jobs[0].status == JobStatus.FAILED


async def test_run_cli_upload_that_expands_too_large_fails_as_source_too_large(
    session_factory: Any,
) -> None:
    archive = _source({"Dockerfile": b"FROM scratch", "data.bin": b"\0" * 50_000})
    job = await _seed_cli_build(session_factory, archive)
    service = _worker(
        session_factory,
        FakeCodeBuild(SUCCEEDED),
        _storage(archive),
        settings=settings_with(upload_max_uncompressed_bytes=10_000),
    )
    job = await _claim(service)

    with pytest.raises(BuildFailedError) as raised:
        await service.run(job, asyncio.Event())

    assert raised.value.failure_code == FailureCode.SOURCE_TOO_LARGE


async def test_run_cli_upload_over_compressed_limit_fails_without_downloading(
    session_factory: Any,
) -> None:
    archive = _source({"Dockerfile": b"FROM scratch"})
    job = await _seed_cli_build(session_factory, archive)
    storage = _storage(archive)
    service = _worker(
        session_factory,
        FakeCodeBuild(SUCCEEDED),
        storage,
        settings=settings_with(snapshot_max_bytes=len(archive) - 1),
    )
    job = await _claim(service)

    with pytest.raises(BuildFailedError) as raised:
        await service.run(job, asyncio.Event())

    assert raised.value.failure_code == FailureCode.SOURCE_TOO_LARGE
    assert storage.downloads == []


@pytest.mark.parametrize(
    ("recorded_sha256", "recorded_size"),
    [("f" * 64, None), (None, 1)],
    ids=["sha256-mismatch", "size-mismatch"],
)
async def test_run_cli_upload_that_differs_from_recorded_checksum_fails_as_invalid(
    session_factory: Any, recorded_sha256: str | None, recorded_size: int | None
) -> None:
    archive = _source({"Dockerfile": b"FROM scratch"})
    job = await _seed_cli_build(
        session_factory, archive, recorded_sha256=recorded_sha256, recorded_size=recorded_size
    )
    service = _worker(session_factory, FakeCodeBuild(SUCCEEDED), _storage(archive))
    job = await _claim(service)

    with pytest.raises(BuildFailedError) as raised:
        await service.run(job, asyncio.Event())

    assert raised.value.failure_code == FailureCode.SOURCE_INVALID


async def test_run_cli_upload_missing_from_storage_fails_as_source_not_found(
    session_factory: Any,
) -> None:
    archive = _source({"Dockerfile": b"FROM scratch"})
    job = await _seed_cli_build(session_factory, archive)
    service = _worker(session_factory, FakeCodeBuild(SUCCEEDED), FakeUploadStorage())
    job = await _claim(service)

    with pytest.raises(BuildFailedError) as raised:
        await service.run(job, asyncio.Event())

    assert raised.value.failure_code == FailureCode.SOURCE_REF_NOT_FOUND


async def test_run_cli_upload_storage_outage_is_retried(session_factory: Any) -> None:
    archive = _source({"Dockerfile": b"FROM scratch"})
    job = await _seed_cli_build(session_factory, archive)
    storage = _storage(archive)
    storage.download_error = ExternalError("aws request failed", operation="download_file")
    service = _worker(session_factory, FakeCodeBuild(SUCCEEDED), storage)
    job = await _claim(service)

    with pytest.raises(ExternalError) as raised:
        await service.run(job, asyncio.Event())
    await service.retry_or_fail(job, raised.value)

    build, _, jobs = await _load(session_factory, job)
    assert jobs[0].status == JobStatus.RETRY_WAIT
    assert build.status == BuildStatus.SNAPSHOTTING


async def test_run_cli_upload_retry_after_outage_succeeds(session_factory: Any) -> None:
    archive = _source({"Dockerfile": b"FROM scratch"})
    job = await _seed_cli_build(session_factory, archive)
    storage = _storage(archive)
    storage.download_error = ExternalError("aws request failed", operation="download_file")
    codebuild = FakeCodeBuild(SUCCEEDED)
    service = _worker(session_factory, codebuild, storage)
    job = await _claim(service)
    with pytest.raises(ExternalError):
        await service.run(job, asyncio.Event())
    storage.download_error = None

    await service.run(job, asyncio.Event())

    build, _, _ = await _load(session_factory, job)
    assert build.status == BuildStatus.SUCCEEDED
    assert len(codebuild.started_envs) == 1


async def test_failure_code_check_constraint_accepts_source_invalid(session_factory: Any) -> None:
    archive = _source({"Dockerfile": b"FROM scratch"})
    await _seed_cli_build(session_factory, archive)

    async with session_factory.begin() as session:
        request = (await session.scalars(select(DeploymentRequest))).one()
        request.failure_code = FailureCode.SOURCE_INVALID

    async with session_factory() as session:
        stored = (await session.scalars(select(DeploymentRequest))).one()
    assert stored.failure_code == FailureCode.SOURCE_INVALID
