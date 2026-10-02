"""Analyze/plan/build/GitOps flow against PostgreSQL, with external cloud calls replaced."""

# ruff: noqa: F811

import asyncio
import io
import json
import os
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import func, select, update

from app.clients.argocd_client import ArgoAppStatus
from app.clients.aws_clients import CodeBuildResult
from app.core.build_config import BuildWorkerSettings
from app.core.deploy_config import DeployWorkerSettings
from app.core.exceptions import ExternalError
from app.core.worker_exceptions import JobLeaseLostError
from app.enums import (
    DeploymentStatus,
    DeploymentTrigger,
    Environment,
    FailureCode,
    JobKind,
    JobStatus,
    ReleaseStatus,
)
from app.main import app
from app.models.build import Build
from app.models.deployment_diagnosis import DeploymentDiagnosis
from app.models.deployment_request import DeploymentRequest
from app.models.deployment_status_history import DeploymentStatusHistory
from app.models.job import Job
from app.models.release import Release
from app.repositories.job_repository import JobRepository
from app.schemas.build_worker import BuildConfig, BuildJobPayload
from app.services.build_service import BuildService, package_source, render_buildspec
from app.services.deploy_service import (
    DeployService,
    Verdict,
    evaluate_release,
    render_service_values,
)
from tests.test_analysis_integration import Setup, setup  # noqa: F401
from tests.test_pipeline_integration import pipeline_setup  # noqa: F401

integration = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set"
)
DIGEST = "sha256:" + "a" * 64
REPOSITORY = "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/iris/services/1"


class CodeBuild:
    def __init__(self, status: str = "SUCCEEDED") -> None:
        self.status = status
        self.starts = 0
        self.lose_start_response = False
        self.env: dict[str, str] = {}

    async def start_build(
        self, env: dict[str, str], token: str, timeout_minutes: int, buildspec: str
    ) -> str:
        self.starts += 1
        self.env = env
        assert json.loads(buildspec)["phases"]["build"]
        if self.lose_start_response:
            self.lose_start_response = False
            raise ExternalError("simulated lost StartBuild response")
        return "iris-test:build-1"

    async def get_build(self, build_id: str) -> CodeBuildResult:
        return CodeBuildResult(self.status, "BUILD" if self.status == "FAILED" else None, None)

    async def find_build_for_request(self, request_id: str) -> str | None:
        return "iris-test:build-1" if self.starts else None


class Ecr:
    async def tag_image(self, repository_name: str, image_digest: str, image_tag: str) -> None:
        assert image_tag.startswith("r-") and image_digest == DIGEST

    async def ensure_repository(self, service_id: int) -> str:
        return REPOSITORY.replace("/services/1", f"/services/{service_id}")

    async def get_image_digest(self, repository_name: str, image_tag: str) -> str:
        return DIGEST


class Artifacts:
    def __init__(self) -> None:
        self.contents = b""

    async def put_snapshot(self, build_id: int, path: Path, digest: str) -> str:
        self.contents = await asyncio.to_thread(path.read_bytes)
        with tarfile.open(fileobj=io.BytesIO(self.contents), mode="r:gz") as stream:
            assert "source/.env" not in stream.getnames()
        return f"snapshot/{build_id}/{digest}.tar.gz"

    async def presign(self, key: str) -> str:
        return "https://example.test/source"


class GitOps:
    def __init__(self) -> None:
        self.head = "b" * 40
        self.commits = 0
        self.pushes = 0
        self.contents: dict[str, dict[str, Any]] = {}
        self.parents: dict[str, str] = {}
        self.lose_push_response = False
        self.newer_values = False

    async def get_head_sha(self) -> str:
        return self.head

    async def contains(self, commit_sha: str, head_sha: str | None) -> bool:
        while head_sha is not None:
            if commit_sha == head_sha:
                return True
            head_sha = self.parents.get(head_sha)
        return False

    async def create_values_commit(self, parent: str, path: str, values: str, message: str) -> str:
        self.commits += 1
        sha = f"{self.commits:040x}"
        self.parents[sha] = parent
        self.contents[sha] = json.loads(values)
        assert path.startswith("services/") and path.endswith("/prod")
        assert "Iris-Release-Id:" in message
        return sha

    async def update_branch(self, sha: str) -> None:
        self.head = sha
        self.pushes += 1
        if self.lose_push_response:
            self.lose_push_response = False
            raise ExternalError("simulated lost push response")

    async def find_values(self, sha: str, path: str) -> dict[str, Any] | None:
        return self.contents.get(sha)

    async def find_subtree_sha(self, sha: str, path: str) -> str | None:
        return "newer-tree" if self.newer_values and sha == self.head else sha

    async def create_subtree_commit(self, parent: str, path: str, tree: str, message: str) -> str:
        self.commits += 1
        sha = f"{self.commits:040x}"
        self.parents[sha] = parent
        self.contents[sha] = self.contents[tree]
        return sha


class Argo:
    def __init__(self, git: GitOps, healthy: bool = True) -> None:
        self.git = git
        self.healthy = healthy
        self.stale = False

    async def get_application(self, name: str, refresh: bool) -> ArgoAppStatus:
        assert name.startswith("svc-")
        revision = "f" * 40 if self.stale else self.git.head
        return ArgoAppStatus(
            "Synced",
            revision,
            "Healthy" if self.healthy else "Degraded",
            "Succeeded" if self.healthy else "Failed",
            revision,
            None if self.healthy else "container failed to start; token=super-private-value",
        )


def workers(
    state: Setup, codebuild: CodeBuild, git: GitOps, argo: Argo
) -> tuple[BuildService, DeployService]:
    build = BuildService(
        state.factory,
        state.source,
        codebuild,  # type: ignore[arg-type]
        Ecr(),  # type: ignore[arg-type]
        Artifacts(),  # type: ignore[arg-type]
        BuildWorkerSettings(
            _env_file=None,
            github_app_id="test",
            github_app_private_key="test",
            aws_region="ap-northeast-2",
            codebuild_project="iris-test",
            artifact_bucket="iris-test",
        ),
        "build-test-worker",
    )
    deploy = DeployService(
        state.factory,
        git,  # type: ignore[arg-type]
        argo,  # type: ignore[arg-type]
        DeployWorkerSettings(
            _env_file=None,
            aws_region="ap-northeast-2",
            base_domain="example.test",
            gitops_repository="test/gitops",
            gitops_app_id="test",
            gitops_app_private_key="test",
            gitops_installation_id=1,
            argocd_server_url="https://argo.example.test",
            argocd_token="test",
        ),
        "deploy-test-worker",
        Ecr(),  # type: ignore[arg-type]
    )
    return build, deploy


async def start_pipeline(state: Setup, coordinator: Any) -> dict[str, Any]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        url = f"/api/v1/services/{state.service.id}/pipelines"
        created = await http.post(url, json={"mode": "static"})
        assert created.status_code == 202, created.text
        await coordinator.tick()
        await state.execute()
        await coordinator.tick()
        run = (await http.get(url)).json()["data"]
        assert run["status"] == "BUILDING", run
        return run


@pytest.mark.integration
@integration
async def test_one_click_engine_reaches_verified_digest_and_succeeded_revision(
    pipeline_setup: Any,
) -> None:
    state, coordinator = pipeline_setup
    run = await start_pipeline(state, coordinator)
    codebuild, git = CodeBuild(), GitOps()
    build, deploy = workers(state, codebuild, git, Argo(git))
    job = await build.claim_next_job()
    assert job is not None
    await build.process_job(job, asyncio.Event())
    assert codebuild.starts == 1
    assert json.loads(codebuild.env["BUILD_VARIABLES_JSON"]) == {"PORT": "9999"}
    async with state.factory() as session:
        result = await session.scalar(select(Build))
        assert result is not None and result.image_digest == DIGEST
        assert result.source_sha == state.source.sha and result.source_archive_digest
        request = await session.get(DeploymentRequest, run["deploymentRequestId"])
        assert request is not None and request.status == DeploymentStatus.DEPLOYING
    job = await deploy.claim_next_job()
    assert job is not None and job.kind == JobKind.DEPLOY
    await deploy.process_job(job)
    job = await deploy.claim_next_job()
    assert job is not None and job.kind == JobKind.RECONCILE
    await deploy.process_job(job)
    async with state.factory() as session:
        request = await session.get(DeploymentRequest, run["deploymentRequestId"])
        assert request is not None and request.status == DeploymentStatus.SUCCEEDED
        histories = list((await session.scalars(select(DeploymentStatusHistory))).all())
        assert [history.to_status for history in histories] == [
            DeploymentStatus.QUEUED,
            DeploymentStatus.BUILDING,
            DeploymentStatus.DEPLOYING,
            DeploymentStatus.SUCCEEDED,
        ]
        assert await session.scalar(select(func.count()).select_from(DeploymentDiagnosis)) == 0
    await coordinator.tick()
    assert git.commits == git.pushes == 1


@pytest.mark.integration
@integration
async def test_build_failure_keeps_gitops_untouched_and_enqueues_diagnosis(
    pipeline_setup: Any,
) -> None:
    state, coordinator = pipeline_setup
    run = await start_pipeline(state, coordinator)
    codebuild, git = CodeBuild("FAILED"), GitOps()
    build, _ = workers(state, codebuild, git, Argo(git))
    job = await build.claim_next_job()
    assert job is not None
    await build.process_job(job, asyncio.Event())
    async with state.factory() as session:
        request = await session.get(DeploymentRequest, run["deploymentRequestId"])
        assert request is not None and request.status == DeploymentStatus.FAILED
        assert request.failure_code == FailureCode.BUILD_FAILED
        assert await session.scalar(select(func.count()).select_from(DeploymentDiagnosis)) == 1
        assert await session.scalar(select(func.count()).select_from(Release)) == 0
    await coordinator.tick()
    assert git.commits == git.pushes == 0


@pytest.mark.integration
@integration
async def test_deploy_failure_records_masked_actual_argo_evidence_and_blocks_unsafe_rollback(
    pipeline_setup: Any,
) -> None:
    state, coordinator = pipeline_setup
    run = await start_pipeline(state, coordinator)
    codebuild, git = CodeBuild(), GitOps()
    build, deploy = workers(state, codebuild, git, Argo(git, healthy=False))
    job = await build.claim_next_job()
    assert job is not None
    await build.process_job(job, asyncio.Event())
    job = await deploy.claim_next_job()
    assert job is not None
    await deploy.process_job(job)
    job = await deploy.claim_next_job()
    assert job is not None
    await deploy.process_job(job)
    async with state.factory() as session:
        completed = await session.get(Job, job.id)
        assert completed is not None
        assert "super-private-value" not in json.dumps(completed.payload)
        assert completed.payload["failureLog"]["artifactRef"] == git.head
        request = await session.get(DeploymentRequest, run["deploymentRequestId"])
        assert request is not None and request.status == DeploymentStatus.FAILED
        assert await session.scalar(select(func.count()).select_from(DeploymentDiagnosis)) == 1
    rollback = await deploy.claim_next_job()
    assert rollback is not None and rollback.kind == JobKind.ROLLBACK
    await deploy.process_job(rollback)
    async with state.factory() as session:
        request = await session.get(DeploymentRequest, run["deploymentRequestId"])
        assert request is not None and request.status == DeploymentStatus.MANUAL_INTERVENTION
    assert git.commits == 1


@pytest.mark.integration
@integration
async def test_lost_gitops_push_response_resumes_recorded_commit_without_duplicate(
    pipeline_setup: Any,
) -> None:
    state, coordinator = pipeline_setup
    await start_pipeline(state, coordinator)
    codebuild, git = CodeBuild(), GitOps()
    build, deploy = workers(state, codebuild, git, Argo(git))
    job = await build.claim_next_job()
    assert job is not None
    await build.process_job(job, asyncio.Event())
    git.lose_push_response = True
    job = await deploy.claim_next_job()
    assert job is not None
    await deploy.process_job(job)
    async with state.factory.begin() as session:
        await session.execute(update(Job).where(Job.id == job.id).values(run_after=func.now()))
    resumed = await deploy.claim_next_job()
    assert resumed is not None and resumed.id == job.id
    await deploy.process_job(resumed)
    assert git.commits == git.pushes == 1


@pytest.mark.integration
@integration
async def test_expired_claim_is_fenced_after_another_worker_reclaims(pipeline_setup: Any) -> None:
    state, coordinator = pipeline_setup
    await start_pipeline(state, coordinator)
    async with state.factory.begin() as session:
        old = await JobRepository(session).claim_next_job("worker-old", frozenset({JobKind.BUILD}))
        assert old is not None
    async with state.factory.begin() as session:
        await session.execute(
            update(Job)
            .where(Job.id == old.id)
            .values(locked_until=datetime.now(UTC) - timedelta(seconds=1))
        )
    async with state.factory.begin() as session:
        new = await JobRepository(session).claim_next_job("worker-new", frozenset({JobKind.BUILD}))
        assert new is not None and new.id == old.id and new.attempts == old.attempts + 1
    async with state.factory.begin() as session:
        with pytest.raises(JobLeaseLostError):
            await JobRepository(session).record_external_id(old, "worker-old", "stale-result")
    async with state.factory() as session:
        saved = await session.get(Job, old.id)
        assert saved is not None and saved.external_id is None and saved.status == JobStatus.RUNNING


def test_stale_argo_failure_is_ignored_until_target_revision_is_observed() -> None:
    now = datetime.now(UTC)
    status = ArgoAppStatus("Synced", "old", "Degraded", "Failed", "old", "old failure")
    assert evaluate_release(status, False, False, now, now + timedelta(seconds=10)) == Verdict.WAIT
    assert evaluate_release(status, True, True, now, now + timedelta(seconds=10)) == Verdict.FAILED
    assert (
        evaluate_release(None, False, False, now, now - timedelta(seconds=1)) == Verdict.TIMED_OUT
    )


def test_values_use_digest_port_commands_public_variables_and_existing_secret_reference() -> None:
    config = BuildConfig(
        builder="railpack",
        port=9123,
        start_command="node app.js --port $PORT",
        runtime_env=[
            {"key": "PORT", "value": "9123"},
            {"key": "MODE", "value": "production"},
            {"key": "DATABASE_URL", "secret_ref": "app-runtime", "secret_key": "url"},
        ],
    )
    values = json.loads(
        render_service_values(
            service_id=1,
            target_id=2,
            release_id=3,
            image_repository=REPOSITORY,
            image_digest=DIGEST,
            source_sha="a" * 40,
            config=config,
            domain_suffix="example.test",
        )
    )
    assert values["image"] == {"repository": REPOSITORY, "digest": DIGEST}
    assert values["containerPort"] == 9123
    assert values["command"] == ["/bin/sh", "-c", "node app.js --port $PORT"]
    assert values["environment"] == [
        {"name": "MODE", "value": "production"},
        {
            "name": "DATABASE_URL",
            "valueFrom": {"secretKeyRef": {"name": "app-runtime", "key": "url"}},
        },
    ]


def test_snapshot_archive_is_reproducible_and_does_not_include_credentials(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "Dockerfile").write_text("FROM scratch\n")
    (root / ".env").write_text("TOKEN=secret")
    payload = BuildJobPayload(
        source_repository_url="https://github.com/test/app",
        source_branch="main",
        source_sha="a" * 40,
        build_config={"builder": "dockerfile", "dockerfile_path": "Dockerfile", "port": 8080},
    )
    first = package_source(root, tmp_path / "first.tar.gz", payload)
    second = package_source(root, tmp_path / "second.tar.gz", payload)
    assert first == second
    with tarfile.open(tmp_path / "first.tar.gz", "r:gz") as archive:
        assert archive.getnames() == ["source/Dockerfile"]
    with pytest.raises(ValidationError):
        payload.model_validate({**payload.model_dump(), "root_directory": "../external"})


def test_buildspec_uses_checksum_and_argument_arrays_without_evaluating_commands() -> None:
    plan = json.loads(render_buildspec())
    assert "sha256sum -c" in plan["phases"]["install"]["commands"][0]
    prepare = plan["phases"]["pre_build"]["commands"][0]
    build = plan["phases"]["build"]["commands"][0]
    assert 'args+=(--start-cmd "$RAILPACK_START_CMD")' in prepare
    assert 'args+=(--env "$binding")' in prepare
    assert 'args+=(--build-arg "$binding")' in build
    assert "railpack-frontend:v${RAILPACK_VERSION}" in build


@pytest.mark.integration
@integration
async def test_lost_codebuild_start_response_recovers_external_build_without_second_start(
    pipeline_setup: Any,
) -> None:
    state, coordinator = pipeline_setup
    run = await start_pipeline(state, coordinator)
    codebuild, git = CodeBuild(), GitOps()
    codebuild.lose_start_response = True
    build, _ = workers(state, codebuild, git, Argo(git))
    job = await build.claim_next_job()
    assert job is not None
    await build.process_job(job, asyncio.Event())
    async with state.factory.begin() as session:
        saved = await session.get(Job, job.id)
        assert saved is not None and saved.status == JobStatus.RETRY_WAIT
        assert saved.payload["codebuild_start_requested"] is True
        saved.run_after = datetime.now(UTC)
    resumed = await build.claim_next_job()
    assert resumed is not None and resumed.id == job.id
    await build.process_job(resumed, asyncio.Event())
    async with state.factory() as session:
        request = await session.get(DeploymentRequest, run["deploymentRequestId"])
        assert request is not None and request.status == DeploymentStatus.DEPLOYING
    assert codebuild.starts == 1


@pytest.mark.integration
@integration
async def test_queue_config_tampering_is_rejected_before_codebuild_submission(
    pipeline_setup: Any,
) -> None:
    state, coordinator = pipeline_setup
    run = await start_pipeline(state, coordinator)
    async with state.factory.begin() as session:
        job = await session.scalar(select(Job))
        assert job is not None
        changed = dict(job.payload)
        changed["build_config"] = {**changed["build_config"], "start_command": "tampered command"}
        job.payload = changed
    codebuild, git = CodeBuild(), GitOps()
    build, _ = workers(state, codebuild, git, Argo(git))
    job = await build.claim_next_job()
    assert job is not None
    await build.process_job(job, asyncio.Event())
    async with state.factory() as session:
        request = await session.get(DeploymentRequest, run["deploymentRequestId"])
        assert request is not None and request.failure_code == FailureCode.BUILD_CONFIG_REQUIRED
    assert codebuild.starts == 0


@pytest.mark.integration
@integration
async def test_rollback_restores_previous_manifest_and_waits_for_its_healthy_revision(
    pipeline_setup: Any,
) -> None:
    state, coordinator = pipeline_setup
    run = await start_pipeline(state, coordinator)
    previous_digest = "sha256:" + "b" * 64
    async with state.factory.begin() as session:
        request = DeploymentRequest(
            service_id=state.service.id,
            environment=Environment.PROD,
            source_sha="f" * 40,
            trigger_type=DeploymentTrigger.MANUAL,
            idempotency_key="old-good-" + run["id"],
            requested_by=state.user.id,
            status=DeploymentStatus.SUCCEEDED,
        )
        session.add(request)
        await session.flush()
        request_now = await session.get(DeploymentRequest, run["deploymentRequestId"])
        assert request_now is not None
        job = await session.scalar(select(Job))
        assert job is not None
        previous = Release(
            deployment_request_id=request.id,
            service_id=state.service.id,
            environment=Environment.PROD,
            target_id=job.payload["target_ids"][0],
            image_digest=previous_digest,
            image_repository=REPOSITORY,
            gitops_commit_sha="b" * 40,
            status=ReleaseStatus.SUCCEEDED,
        )
        session.add(previous)
    codebuild, git = CodeBuild(), GitOps()
    git.contents[git.head] = {"image": {"repository": REPOSITORY, "digest": previous_digest}}
    argo = Argo(git, healthy=False)
    build, deploy = workers(state, codebuild, git, argo)
    job = await build.claim_next_job()
    assert job is not None
    await build.process_job(job, asyncio.Event())
    for _ in range(2):
        job = await deploy.claim_next_job()
        assert job is not None
        await deploy.process_job(job)
    argo.healthy = True
    argo.stale = True
    rollback = await deploy.claim_next_job()
    assert rollback is not None and rollback.kind == JobKind.ROLLBACK
    await deploy.process_job(rollback)
    async with state.factory.begin() as session:
        request = await session.get(DeploymentRequest, run["deploymentRequestId"])
        assert request is not None and request.status == DeploymentStatus.FAILED
        saved = await session.get(Job, rollback.id)
        assert saved is not None and saved.status == JobStatus.QUEUED
        saved.run_after = datetime.now(UTC)
    argo.stale = False
    resumed = await deploy.claim_next_job()
    assert resumed is not None and resumed.id == rollback.id
    await deploy.process_job(resumed)
    async with state.factory() as session:
        request = await session.get(DeploymentRequest, run["deploymentRequestId"])
        assert request is not None and request.status == DeploymentStatus.ROLLED_BACK
        assert await session.scalar(select(func.count()).select_from(DeploymentDiagnosis)) == 1
    assert git.commits == git.pushes == 2
    assert git.contents[git.head]["image"]["digest"] == previous_digest


@pytest.mark.integration
@integration
async def test_changed_source_content_under_the_same_revision_is_rejected_before_build(
    pipeline_setup: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, coordinator = pipeline_setup
    run = await start_pipeline(state, coordinator)
    original_fetch = state.source.fetch_source

    async def changed_fetch(
        repository_url: str, source_sha: str, installation_id: int, destination: Path
    ) -> Path:
        root = await original_fetch(repository_url, source_sha, installation_id, destination)
        await asyncio.to_thread((root / "index.js").write_text, "console.log('changed source');")
        return root

    monkeypatch.setattr(state.source, "fetch_source", changed_fetch)
    codebuild, git = CodeBuild(), GitOps()
    build, _ = workers(state, codebuild, git, Argo(git))
    job = await build.claim_next_job()
    assert job is not None
    await build.process_job(job, asyncio.Event())
    async with state.factory() as session:
        request = await session.get(DeploymentRequest, run["deploymentRequestId"])
        assert request is not None and request.failure_code == FailureCode.BUILD_CONFIG_REQUIRED
    assert codebuild.starts == 0
