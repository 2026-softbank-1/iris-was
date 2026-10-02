"""Execute an immutable analyzer plan and hand one image digest to the deployment queue."""

import asyncio
import gzip
import hashlib
import json
import logging
import re
import tarfile
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.analysis_source_client import AnalysisSourceClient, _is_excluded
from app.clients.aws_clients import ArtifactStore, CodeBuildClient, EcrClient
from app.core.async_io import run_sync
from app.core.build_config import BuildWorkerSettings
from app.core.exceptions import AppError, ExternalError, InvalidInputError, NotConfiguredError
from app.core.worker_exceptions import BuildExecutionError, JobLeaseLostError
from app.enums import Builder, DeploymentStatus, FailureCode, JobKind, PipelineStatus
from app.models.build import Build
from app.models.job import Job
from app.repositories.build_repository import BuildRepository
from app.repositories.deployment_request_repository import DeploymentRequestRepository
from app.repositories.deployment_status_history_repository import DeploymentStatusHistoryRepository
from app.repositories.job_repository import JobRepository
from app.schemas.build_worker import BuildConfig, BuildJobPayload
from app.schemas.pipeline import PipelineVariable
from app.services.deployment_status_service import DeploymentStatusService

logger = logging.getLogger(__name__)
JOB_KINDS = frozenset({JobKind.BUILD})


class BuildService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        source: AnalysisSourceClient,
        codebuild: CodeBuildClient,
        ecr: EcrClient,
        artifacts: ArtifactStore,
        settings: BuildWorkerSettings,
        worker_id: str,
    ) -> None:
        self._sessions = session_factory
        self._source = source
        self._codebuild = codebuild
        self._ecr = ecr
        self._artifacts = artifacts
        self._settings = settings
        self._worker_id = worker_id

    async def claim_next_job(self) -> Job | None:
        async with self._sessions.begin() as session:
            return await JobRepository(session).claim_next_job(
                self._worker_id, JOB_KINDS, self._settings.lease_seconds
            )

    async def process_job(self, job: Job, stop: asyncio.Event) -> None:
        task = asyncio.create_task(self.run(job, stop))
        heartbeat = asyncio.create_task(self._heartbeat(job, task))
        try:
            await task
        except JobLeaseLostError:
            logger.warning("job lease lost", extra={"action": "process_build"})
        except BuildExecutionError as error:
            await self._fail(job, error.failure_code, error.code)
        except AppError as error:
            if error.retryable:
                await self.retry_or_fail(job, error.code)
            else:
                await self._fail(job, FailureCode.BUILD_CONFIG_REQUIRED, error.code)
        except asyncio.CancelledError:
            await asyncio.gather(task, return_exceptions=True)
            await self._release(job)
            raise
        except Exception:
            # Source, SDK and model exception messages can contain credentials.
            logger.error("build execution failed", extra={"action": "process_build"})
            await self.retry_or_fail(job, "BUILD_WORKER_ERROR")
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def run(self, job: Job, stop: asyncio.Event) -> None:
        if job.attempts > job.max_attempts:
            raise BuildExecutionError(FailureCode.BUILD_FAILED)
        try:
            payload = BuildJobPayload.model_validate(job.payload)
        except ValidationError:
            raise BuildExecutionError(FailureCode.BUILD_CONFIG_REQUIRED) from None
        build = await self._get_or_create_build(job, payload)
        if build.image_digest is not None:
            await self._finish(job, build.id, payload, build.image_digest)
            return
        if build.codebuild_build_id is None:
            build = await self._start_build(job, build, payload)
        assert build.codebuild_build_id is not None
        while not stop.is_set():
            result = await self._codebuild.get_build(build.codebuild_build_id)
            async with self._sessions.begin() as session:
                await JobRepository(session).get_owned_job(job, self._worker_id)
                current = await BuildRepository(session).get_by_id(build.id, for_update=True)
                current.log_url = result.log_url
            if result.status == "SUCCEEDED":
                assert build.image_repository is not None
                digest = await self._ecr.get_image_digest(
                    build.image_repository.split("/", 1)[1], f"b-{build.id}"
                )
                if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
                    raise ExternalError("build image digest is invalid")
                await self._finish(job, build.id, payload, digest)
                return
            if result.status != "IN_PROGRESS":
                # A completed execution is terminal; retried queue deliveries resume that same ID.
                raise BuildExecutionError(
                    FailureCode.BUILD_CONFIG_REQUIRED
                    if result.failed_phase == "PRE_BUILD"
                    else FailureCode.BUILD_FAILED
                )
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._settings.poll_interval_seconds)
            except TimeoutError:
                continue
        await self._release(job)

    async def retry_or_fail(self, job: Job, error_code: str) -> None:
        if job.attempts >= job.max_attempts:
            await self._fail(job, FailureCode.BUILD_FAILED, error_code)
            return
        async with self._sessions.begin() as session:
            await JobRepository(session).retry_later(
                job,
                self._worker_id,
                error_code,
                timedelta(seconds=30 * 2 ** min(job.attempts - 1, 5)),
            )

    async def _get_or_create_build(self, job: Job, payload: BuildJobPayload) -> Build:
        async with self._sessions.begin() as session:
            await JobRepository(session).get_owned_job(job, self._worker_id)
            request = await DeploymentRequestRepository(session).get_by_id_for_update(
                job.deployment_request_id
            )
            if request.source_sha != payload.source_sha:
                raise BuildExecutionError(FailureCode.BUILD_CONFIG_REQUIRED)
            await self._validate_plan(session, request.service_id, payload, job)
            builds = BuildRepository(session)
            build = await builds.find_by_deployment_request_id(request.id)
            if build is None:
                if request.status != DeploymentStatus.QUEUED:
                    raise BuildExecutionError(FailureCode.BUILD_CONFIG_REQUIRED)
                build = await builds.save(
                    Build(
                        deployment_request_id=request.id,
                        builder=payload.build_config.builder,
                        source_sha=payload.source_sha,
                        build_config=payload.build_config.model_dump(mode="json"),
                        started_at=datetime.now(UTC),
                    )
                )
                await _statuses(session).transition_status(request.id, DeploymentStatus.BUILDING)
            elif (
                build.source_sha != payload.source_sha
                or build.build_config != payload.build_config.model_dump(mode="json")
            ):
                raise BuildExecutionError(FailureCode.BUILD_CONFIG_REQUIRED)
            return build

    async def _validate_plan(
        self, session: AsyncSession, service_id: int, payload: BuildJobPayload, job: Job
    ) -> None:
        # This import also keeps worker-only orchestration out of API dependency assembly.
        from app.models.pipeline_run import PipelineRun
        from app.services.pipeline_contract import digest_pipeline_plan, validate_pipeline_plan

        pipeline_id = job.payload.get("pipeline_run_id") or payload.build_config.pipeline_run_id
        digest = (
            job.payload.get("analysis_plan_digest") or payload.build_config.analysis_plan_digest
        )
        run = await session.get(PipelineRun, pipeline_id)
        if run is None or run.execution_plan is None:
            raise BuildExecutionError(FailureCode.BUILD_CONFIG_REQUIRED)
        validate_pipeline_plan(run.execution_plan)
        if (
            run.service_id != service_id
            or run.source_sha != payload.source_sha
            or run.plan_digest != digest
            or digest_pipeline_plan(run.execution_plan) != digest
            or run.status not in {PipelineStatus.BUILDING, PipelineStatus.DEPLOYING}
            or run.questions
            or run.execution_plan["sourceRepositoryUrl"] != payload.source_repository_url
            or run.execution_plan["sourceSha"] != payload.source_sha
            or run.execution_plan["serviceId"] != service_id
            or run.execution_plan["pipelineRunId"] != pipeline_id
            or run.execution_plan["targetIds"] != payload.target_ids
            or run.target_ids != payload.target_ids
            or run.github_installation_id != payload.github_installation_id
        ):
            raise BuildExecutionError(FailureCode.BUILD_CONFIG_REQUIRED)
        # The root-owned contract validates source/config/targets against this immutable run.
        expected = BuildConfig.model_validate(run.execution_plan["buildConfig"])
        actual = payload.build_config.model_copy(update={"analysis_plan_digest": None})
        if (
            actual.model_dump(mode="json") != expected.model_dump(mode="json")
            or expected.root_directory != payload.root_directory
            or expected.pipeline_run_id != pipeline_id
        ):
            raise BuildExecutionError(FailureCode.BUILD_CONFIG_REQUIRED)

    async def _start_build(self, job: Job, build: Build, payload: BuildJobPayload) -> Build:
        # A write intent is persisted before StartBuild. On response loss, discovery resumes it.
        async with self._sessions.begin() as session:
            from app.models.pipeline_run import PipelineRun

            current_job = await JobRepository(session).get_owned_job(job, self._worker_id)
            is_recovery = current_job.payload.get("codebuild_start_requested") is True
            run = await session.get(PipelineRun, job.payload.get("pipeline_run_id"))
            if run is None or run.execution_plan is None:
                raise BuildExecutionError(FailureCode.BUILD_CONFIG_REQUIRED)
            snapshot_id = str(run.execution_plan["sourceSnapshotId"])
        if is_recovery:
            build_id = await self._codebuild.find_build_for_request(str(build.id))
            if build_id is None:
                raise ExternalError("codebuild start outcome is pending recovery")
            return await self._record_codebuild(job, build.id, build_id)
        if payload.github_installation_id is None:
            raise BuildExecutionError(FailureCode.BUILD_CONFIG_REQUIRED)
        temporary = await run_sync(lambda: tempfile.TemporaryDirectory(prefix="iris-build-"))
        try:
            source_path = await run_sync(lambda: Path(temporary.name).resolve() / "source")
            source = await self._source.fetch_source(
                payload.source_repository_url,
                payload.source_sha,
                payload.github_installation_id,
                source_path,
            )
            actual_snapshot = await run_sync(lambda: get_analyzer_snapshot_id(source))
            if actual_snapshot != snapshot_id:
                raise BuildExecutionError(FailureCode.BUILD_CONFIG_REQUIRED)
            archive = Path(temporary.name) / "source.tar.gz"
            archive_digest = await run_sync(lambda: package_source(source, archive, payload))
            snapshot_key = await self._artifacts.put_snapshot(build.id, archive, archive_digest)
        finally:
            await run_sync(temporary.cleanup)
        repository = await self._get_image_repository(job, build.id)
        config = payload.build_config
        railpack_version = config.railpack_version or self._settings.railpack_version
        if config.builder == Builder.RAILPACK and railpack_version is None:
            raise BuildExecutionError(FailureCode.BUILD_CONFIG_REQUIRED)
        env = {
            "IRIS_BUILD_ID": str(build.id),
            "SOURCE_BUCKET": self._settings.artifact_bucket,
            "SOURCE_KEY": snapshot_key,
            "SOURCE_SHA": payload.source_sha,
            "SOURCE_ARCHIVE_DIGEST": archive_digest,
            "SOURCE_URL": await self._artifacts.presign(snapshot_key),
            "ROOT_DIRECTORY": payload.root_directory,
            "BUILDER": config.builder.value,
            "DOCKERFILE_PATH": config.dockerfile_path or "",
            "PLATFORM": config.platform,
            "REGISTRY": repository.split("/", 1)[0],
            "IMAGE_REPO": repository,
            "IMAGE_TAG": f"b-{build.id}",
            "RAILPACK_VERSION": railpack_version or "",
            "RAILPACK_BUILD_CMD": config.build_command or "",
            "RAILPACK_START_CMD": config.start_command or "",
            "PORT": str(config.port),
            "BUILD_VARIABLES_JSON": json.dumps(public_build_variables(config)),
        }
        async with self._sessions.begin() as session:
            current_job = await JobRepository(session).get_owned_job(job, self._worker_id)
            current = await BuildRepository(session).get_by_id(build.id, for_update=True)
            request = await DeploymentRequestRepository(session).get_by_id_for_update(
                job.deployment_request_id
            )
            env["SERVICE_ID"] = str(request.service_id)
            if config.builder == Builder.RAILPACK:
                checksum = self._settings.railpack_sha256
                if checksum is None and railpack_version == "0.40.1":
                    checksum = "2842de93e68713af9037e0bc0a398d7da78f3b96aa4804303a638db2bc69bd30"
                if checksum is None:
                    raise BuildExecutionError(FailureCode.BUILD_CONFIG_REQUIRED)
                env["RAILPACK_SHA256"] = checksum
            current.source_snapshot_key = snapshot_key
            current.source_archive_digest = archive_digest
            current.image_repository = repository
            current_job.payload = {**current_job.payload, "codebuild_start_requested": True}
        build_id = await self._codebuild.start_build(
            env, f"iris-build-{build.id}", self._settings.build_timeout_minutes, render_buildspec()
        )
        return await self._record_codebuild(job, build.id, build_id)

    async def _get_image_repository(self, job: Job, build_id: int) -> str:
        async with self._sessions.begin() as session:
            await JobRepository(session).get_owned_job(job, self._worker_id)
            request = await DeploymentRequestRepository(session).get_by_id_for_update(
                job.deployment_request_id
            )
            service_id = request.service_id
        return await self._ecr.ensure_repository(service_id)

    async def _record_codebuild(self, job: Job, build_id: int, external_id: str) -> Build:
        async with self._sessions.begin() as session:
            jobs = JobRepository(session)
            await jobs.get_owned_job(job, self._worker_id)
            build = await BuildRepository(session).get_by_id(build_id, for_update=True)
            build.codebuild_build_id = external_id
            await jobs.record_external_id(job, self._worker_id, external_id)
            return build

    async def _finish(self, job: Job, build_id: int, payload: BuildJobPayload, digest: str) -> None:
        async with self._sessions.begin() as session:
            jobs = JobRepository(session)
            await jobs.get_owned_job(job, self._worker_id)
            build = await BuildRepository(session).get_by_id(build_id, for_update=True)
            request = await DeploymentRequestRepository(session).get_by_id_for_update(
                job.deployment_request_id
            )
            if request.status == DeploymentStatus.BUILDING:
                build.image_digest = digest
                build.finished_at = datetime.now(UTC)
                await jobs.save(
                    Job(
                        deployment_request_id=request.id,
                        kind=JobKind.DEPLOY,
                        payload={
                            **payload.model_dump(mode="json"),
                            "build_id": build.id,
                            "pipeline_run_id": job.payload.get("pipeline_run_id"),
                            "analysis_plan_digest": job.payload.get("analysis_plan_digest"),
                        },
                    )
                )
                await _statuses(session).transition_status(request.id, DeploymentStatus.DEPLOYING)
            await jobs.mark_succeeded(job, self._worker_id)

    async def _fail(self, job: Job, failure_code: FailureCode, error_code: str) -> None:
        try:
            async with self._sessions.begin() as session:
                jobs = JobRepository(session)
                await jobs.get_owned_job(job, self._worker_id)
                request = await DeploymentRequestRepository(session).get_by_id_for_update(
                    job.deployment_request_id
                )
                if request.status in {DeploymentStatus.QUEUED, DeploymentStatus.BUILDING}:
                    build = await BuildRepository(session).find_by_deployment_request_id(request.id)
                    if build is not None:
                        build.finished_at = datetime.now(UTC)
                    await _statuses(session).transition_status(
                        request.id, DeploymentStatus.FAILED, failure_code=failure_code
                    )
                await jobs.mark_failed(job, self._worker_id, error_code)
        except JobLeaseLostError:
            logger.warning("job lease lost", extra={"action": "fail_build"})

    async def _release(self, job: Job) -> None:
        try:
            async with self._sessions.begin() as session:
                await JobRepository(session).release(job, self._worker_id)
        except JobLeaseLostError:
            logger.warning("job lease lost", extra={"action": "release_build"})

    async def _heartbeat(self, job: Job, task: asyncio.Task[None]) -> None:
        try:
            while not task.done():
                await asyncio.sleep(self._settings.lease_seconds / 3)
                async with self._sessions.begin() as session:
                    retained = await JobRepository(session).renew_lease(
                        job, self._worker_id, self._settings.lease_seconds
                    )
                if not retained:
                    task.cancel()
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            task.cancel()
            logger.error("build lease renewal failed", extra={"action": "renew_build_lease"})


def _statuses(session: AsyncSession) -> DeploymentStatusService:
    return DeploymentStatusService(
        DeploymentRequestRepository(session),
        DeploymentStatusHistoryRepository(session),
        session=session,
    )


def get_analyzer_snapshot_id(source: Path) -> str:
    try:
        from iris_analyzer.contracts import Limits
        from iris_analyzer.preprocess.snapshot import capture, release_snapshot
    except ImportError:
        raise NotConfiguredError(
            "build source verification requires the analyzer package"
        ) from None
    snapshot = capture(source, Limits())
    try:
        return str(snapshot.snapshot_id)
    finally:
        release_snapshot(snapshot.snapshot_id)


def package_source(source: Path, archive: Path, payload: BuildJobPayload) -> str:
    root = source.joinpath(payload.root_directory).resolve()
    if not root.is_relative_to(source.resolve()) or not root.is_dir():
        raise InvalidInputError("confirmed service root does not exist")
    if payload.build_config.builder == Builder.DOCKERFILE:
        dockerfile = root / (payload.build_config.dockerfile_path or "")
        if not dockerfile.is_file() or dockerfile.is_symlink():
            raise InvalidInputError("confirmed Dockerfile does not exist")
    # Fixed metadata and gzip mtime make repeated source archives byte-identical.
    with (
        archive.open("xb") as raw,
        gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, filename="") as compressed,
    ):
        with tarfile.open(fileobj=compressed, mode="w") as stream:
            for path in sorted(source.rglob("*")):
                if path.is_symlink():
                    raise InvalidInputError("build source cannot contain links")
                if not path.is_file():
                    continue
                relative = path.relative_to(source).as_posix()
                if _is_excluded(PurePosixPath(relative)):
                    continue
                info = stream.gettarinfo(str(path), arcname=f"source/{relative}")
                info.uid = info.gid = info.mtime = 0
                info.uname = info.gname = ""
                with path.open("rb") as content:
                    stream.addfile(info, content)
    return hashlib.sha256(archive.read_bytes()).hexdigest()


def render_buildspec() -> str:
    """Versioned platform buildspec; confirmed commands are arguments, never shell interpolated."""
    import json

    install = """set -euo pipefail
if [ "$BUILDER" = railpack ]; then
  url="https://github.com/railwayapp/railpack/releases/download/v${RAILPACK_VERSION}"
  curl -fsSL -o /tmp/railpack.tgz \
    "$url/railpack-v${RAILPACK_VERSION}-x86_64-unknown-linux-musl.tar.gz"
  printf '%s  %s\\n' "$RAILPACK_SHA256" /tmp/railpack.tgz | sha256sum -c -
  tar -xzf /tmp/railpack.tgz -C /usr/local/bin railpack
fi
mkdir -p /src
curl -fsSL "$SOURCE_URL" -o /tmp/iris-source.tar.gz
printf '%s  %s\\n' "$SOURCE_ARCHIVE_DIGEST" /tmp/iris-source.tar.gz | sha256sum -c -
tar -xzf /tmp/iris-source.tar.gz --strip-components=1 -C /src
aws ecr get-login-password | docker login -u AWS --password-stdin "$REGISTRY"
docker buildx create --use --driver docker-container
"""
    prepare = """set -euo pipefail
cd "/src/$ROOT_DIRECTORY"
if [ "$BUILDER" = railpack ]; then
  args=()
  while IFS= read -r -d '' binding; do args+=(--env "$binding"); done < <(
    python -c 'import json,os,sys; \
sys.stdout.write("\\0".join(k+"="+v for k,v in \
json.loads(os.environ["BUILD_VARIABLES_JSON"]).items())+"\\0")'
  )
  if [ -n "$RAILPACK_BUILD_CMD" ]; then args+=(--build-cmd "$RAILPACK_BUILD_CMD"); fi
  if [ -n "$RAILPACK_START_CMD" ]; then args+=(--start-cmd "$RAILPACK_START_CMD"); fi
  railpack prepare . --plan-out /tmp/plan.json "${args[@]}"
fi
"""
    build = """set -euo pipefail
cd "/src/$ROOT_DIRECTORY"
cache="type=registry,ref=$IMAGE_REPO:cache"
common=(--platform "$PLATFORM" --push -t "$IMAGE_REPO:$IMAGE_TAG"
  --cache-from "$cache" --cache-to "mode=max,image-manifest=true,oci-mediatypes=true,$cache")
if [ "$BUILDER" = railpack ]; then
  docker buildx build "${common[@]}" \
    --build-arg "BUILDKIT_SYNTAX=ghcr.io/railwayapp/railpack-frontend:v${RAILPACK_VERSION}" \
    --build-arg "cache-key=$SERVICE_ID" -f /tmp/plan.json .
else
  args=()
  while IFS= read -r -d '' binding; do args+=(--build-arg "$binding"); done < <(
    python -c 'import json,os,sys; \
sys.stdout.write("\\0".join(k+"="+v for k,v in \
json.loads(os.environ["BUILD_VARIABLES_JSON"]).items())+"\\0")'
  )
  docker buildx build "${common[@]}" "${args[@]}" -f "$DOCKERFILE_PATH" .
fi
"""
    return json.dumps(
        {
            "version": "0.2",
            "env": {"shell": "bash"},
            "phases": {
                "install": {"commands": [install]},
                "pre_build": {"commands": [prepare]},
                "build": {"commands": [build]},
            },
        }
    )


def public_build_variables(config: BuildConfig) -> dict[str, str]:
    variables: dict[str, str] = {}
    for item in config.runtime_env:
        binding = PipelineVariable.model_validate(item)
        if binding.key.startswith(("AWS_", "CODEBUILD_", "IRIS_")) or binding.key in {
            "SOURCE_URL",
            "SOURCE_KEY",
            "SOURCE_BUCKET",
            "REGISTRY",
            "IMAGE_REPO",
            "IMAGE_TAG",
            "BUILDER",
            "ROOT_DIRECTORY",
            "DOCKERFILE_PATH",
            "PLATFORM",
            "RAILPACK_VERSION",
            "BUILD_VARIABLES_JSON",
            "BUILDKIT_SYNTAX",
        }:
            raise InvalidInputError("build variables cannot override platform execution metadata")
        if binding.value is not None:
            variables[binding.key] = binding.value
    return variables
