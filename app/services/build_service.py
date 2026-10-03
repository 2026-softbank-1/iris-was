"""BUILD job 처리: 소스 스냅샷 → 빌더 결정 → CodeBuild → digest 확인 → DEPLOY 인계.

단계마다 builds 에 기록을 남겨, Worker 가 죽어도 다른 Worker 가 이어서 처리한다.
codebuild_build_id 가 있으면 스냅샷·StartBuild 를 건너뛰고 그 빌드를 기다린다.
"""

import asyncio
import contextlib
import logging
import posixpath
import tarfile
import tempfile
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.aws_clients import (
    ArtifactStore,
    BuildLogClient,
    CodeBuildClient,
    CodeBuildResult,
    EcrClient,
)
from app.clients.github_client import GitHubClient, SourceTooLargeError
from app.core.config import BuildWorkerSettings
from app.core.exceptions import BuildFailedError, ExternalError, ForbiddenError, NotFoundError
from app.enums import DeploymentStatus, FailureCode, JobKind
from app.models import Build, Job
from app.repositories.build_repository import BuildRepository
from app.repositories.github_installation_repository import GithubInstallationRepository
from app.repositories.job_repository import JobRepository
from app.services.build_log import to_log_tail
from app.services.builder_detection import CONFIG_FILE_NAME, detect_builder, parse_iris_config
from app.services.deployment_status_service import DeploymentStatusService
from app.services.repository_url import parse_repository_url
from app.services.source_manifest import compute_snapshot_digests

logger = logging.getLogger(__name__)

JOB_KINDS = frozenset({JobKind.BUILD})
RETRY_BASE_DELAY = timedelta(seconds=30)
_MAX_CONFIG_BYTES = 64 * 1024
# 실패한 빌드에서 CloudWatch 로 읽어 올 마지막 줄 수. 저장 한도는 build_log.to_log_tail 이 정한다.
LOG_TAIL_FETCH_LINES = 300
_MAX_ERROR_LENGTH = 1000
# 사용자 소스·설정이 실행되는 buildspec 단계. 그 밖의 단계 실패는 인프라 오류로 재시도한다.
_FAILURE_CODE_BY_PHASE = {
    "PRE_BUILD": FailureCode.BUILD_CONFIG_REQUIRED,
    "BUILD": FailureCode.BUILD_FAILED,
    "POST_BUILD": FailureCode.BUILD_FAILED,
}


class BuildService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        github: GitHubClient,
        codebuild: CodeBuildClient,
        ecr: EcrClient,
        artifacts: ArtifactStore,
        build_logs: BuildLogClient,
        settings: BuildWorkerSettings,
        worker_id: str,
    ) -> None:
        self._session_factory = session_factory
        self._github = github
        self._codebuild = codebuild
        self._ecr = ecr
        self._artifacts = artifacts
        self._build_logs = build_logs
        self._settings = settings
        self._worker_id = worker_id

    async def claim_next_job(self) -> Job | None:
        async with self._session_factory.begin() as session:
            return await JobRepository(session).claim_next_job(
                self._worker_id, JOB_KINDS, self._settings.user_concurrent_build_limit
            )

    async def find_seconds_until_next_run(self) -> float | None:
        async with self._session_factory() as session:
            return await JobRepository(session).find_seconds_until_next_run(JOB_KINDS)

    async def run(self, job: Job, stop: asyncio.Event) -> None:
        """job 을 끝까지 처리한다. stop 이 켜지면 빌드 대기 중에 job 을 반납하고 돌아온다.

        더 진행할 수 없으면 BuildFailedError, 재시도할 만하면 그 밖의 예외를 던진다.
        """
        if job.attempts > job.max_attempts:
            raise BuildFailedError(FailureCode.BUILD_INFRA_ERROR, "attempts exhausted")
        build = await self._get_build(job)
        if build.is_finished:
            await self._close(job, build.id)
            return
        if build.deployment_request.cancel_requested_at is not None:
            await self._close(job, build.id, cancel=True)
            return
        if build.codebuild_build_id is None:
            build = await self._start_codebuild(job, build)
        await self._wait_for_codebuild(job, build, stop)

    async def fail(self, job: Job, error: BuildFailedError) -> None:
        async with self._session_factory.begin() as session:
            build = await BuildRepository(session).get_by_id(_build_id(job), for_update=True)
            if not build.is_finished:
                build.fail(error.failure_code)
                await DeploymentStatusService.create(session).transition_status(
                    build.deployment_request_id,
                    DeploymentStatus.FAILED,
                    failure_code=error.failure_code,
                )
            await JobRepository(session).mark_failed(job.id, error.message)

    async def retry_or_fail(self, job: Job, error: Exception) -> None:
        if job.attempts >= job.max_attempts:
            await self.fail(job, BuildFailedError(FailureCode.BUILD_INFRA_ERROR, _describe(error)))
            return
        async with self._session_factory.begin() as session:
            await JobRepository(session).retry_later(
                job.id, _describe(error), RETRY_BASE_DELAY * 2 ** (job.attempts - 1)
            )

    async def _start_codebuild(self, job: Job, build: Build) -> Build:
        service = build.deployment_request.service
        owner, repository_name = parse_repository_url(service.source_repository_url)
        repository_full_name = f"{owner}/{repository_name}"
        async with self._session_factory() as session:
            installation = await GithubInstallationRepository(session).get_by_id(
                service.github_installation_id
            )
        with _source_errors(FailureCode.SOURCE_NOT_ACCESSIBLE):
            token = await self._github.create_installation_token(
                installation.installation_id, repository_name
            )
        source_sha = build.source_sha or build.deployment_request.source_sha
        async with self._session_factory.begin() as session:
            (await BuildRepository(session).get_by_id(build.id, for_update=True)).start_snapshot(
                source_sha
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot_path = Path(temp_dir) / "source.tar.gz"
            with _source_errors(FailureCode.SOURCE_REF_NOT_FOUND):
                await self._github.download_tarball(
                    token,
                    repository_full_name,
                    source_sha,
                    snapshot_path,
                    self._settings.snapshot_max_bytes,
                )
            file_names, config_content = await asyncio.to_thread(
                _scan_snapshot, snapshot_path, service.root_directory
            )
            config = parse_iris_config(config_content) if config_content is not None else None
            plan = detect_builder(file_names, config, service)
            # 임시 파일이 있을 때 계산하고, 올린 뒤에 기록한다. 업로드가 실패하면 값을 바꾸지 않아
            # 기록한 해시가 올라가지 않은 파일을 가리키는 일이 없다.
            digests = await asyncio.to_thread(compute_snapshot_digests, snapshot_path)
            snapshot_key = await self._artifacts.put_snapshot(build.id, snapshot_path)
            async with self._session_factory.begin() as session:
                (
                    await BuildRepository(session).get_by_id(build.id, for_update=True)
                ).record_source_digests(digests.archive_sha256, digests.manifest_sha256)

        image_repository = await self._ecr.ensure_repository(service.id)
        image_tag = f"b-{build.id}"
        env = {
            "SOURCE_URL": await self._artifacts.presign(snapshot_key),
            "ROOT_DIRECTORY": _normalize_root(service.root_directory),
            "BUILDER": plan.builder.value,
            "DOCKERFILE_PATH": plan.dockerfile_path,
            "REGISTRY": image_repository.split("/", 1)[0],
            "IMAGE_REPO": image_repository,
            "IMAGE_TAG": image_tag,
            "SERVICE_ID": str(service.id),
            **plan.railpack_env,
        }
        # ponytail: StartBuild 직후 ID 기록 전에 죽고 토큰(5분)도 만료되면 빌드가 중복 실행된다.
        #   두 번째 빌드는 불변 태그 push 에서 실패한다. 겪으면 ListBuildsForProject 로 찾는다.
        codebuild_build_id = await self._codebuild.start_build(
            env, f"build-{build.id}-{build.attempt}", self._settings.build_timeout_minutes
        )
        async with self._session_factory.begin() as session:
            build = await BuildRepository(session).get_by_id(build.id, for_update=True)
            build.start_codebuild(
                codebuild_build_id, plan.builder, image_repository, image_tag, plan.deploy_config
            )
            await DeploymentStatusService.create(session).transition_status(
                build.deployment_request_id, DeploymentStatus.BUILDING
            )
            await JobRepository(session).record_external_id(job.id, codebuild_build_id)
        logger.info(
            "codebuild started",
            extra={
                "action": "start_codebuild",
                "build_id": build.id,
                "codebuild_build_id": codebuild_build_id,
                "builder": plan.builder.value,
            },
        )
        return build

    async def _wait_for_codebuild(self, job: Job, build: Build, stop: asyncio.Event) -> None:
        assert build.codebuild_build_id is not None
        while True:
            result = await self._codebuild.get_build(build.codebuild_build_id)
            async with self._session_factory.begin() as session:
                if not await JobRepository(session).renew_lease(job.id, self._worker_id):
                    logger.warning("job lease lost", extra={"action": "wait_for_codebuild"})
                    return
                current = await BuildRepository(session).get_by_id(build.id)
                current.log_url = result.log_url
                is_cancel_requested = current.deployment_request.cancel_requested_at is not None
            if is_cancel_requested:
                await self._codebuild.stop_build(build.codebuild_build_id)
                await self._close(job, build.id, cancel=True)
                return
            if result.status != "IN_PROGRESS":
                break
            if await _sleep_or_stop(stop, self._settings.poll_interval_seconds):
                async with self._session_factory.begin() as session:
                    await JobRepository(session).release(job.id)
                logger.info("job released", extra={"action": "wait_for_codebuild"})
                return

        if result.status == "SUCCEEDED":
            assert build.image_repository is not None and build.image_tag is not None
            image_digest = await self._ecr.get_image_digest(
                build.image_repository.split("/", 1)[1], build.image_tag
            )
            await self._close(job, build.id, image_digest=image_digest)
        elif result.status == "FAILED" and result.failed_phase in _FAILURE_CODE_BY_PHASE:
            await self._record_log_tail(build.id, result)
            raise BuildFailedError(
                _FAILURE_CODE_BY_PHASE[result.failed_phase],
                f"codebuild failed in {result.failed_phase}",
            )
        elif result.status == "TIMED_OUT":
            await self._record_log_tail(build.id, result)
            raise BuildFailedError(FailureCode.BUILD_TIMED_OUT)
        else:
            # FAULT·외부 STOPPED·인프라 단계 실패: 다음 시도에서 CodeBuild 를 새로 시작한다.
            async with self._session_factory.begin() as session:
                (
                    await BuildRepository(session).get_by_id(build.id, for_update=True)
                ).reset_codebuild()
            raise ExternalError("codebuild fault", codebuild_status=result.status)

    async def _record_log_tail(self, build_id: int, result: CodeBuildResult) -> None:
        """실패한 빌드의 로그 끝부분을 남겨 AI 진단이 쓰게 한다. 못 읽어도 실패 처리는 그대로다."""
        if result.log_group is None or result.log_stream is None:
            return
        try:
            tail = await self._build_logs.fetch_tail(
                result.log_group, result.log_stream, LOG_TAIL_FETCH_LINES
            )
        except ExternalError:
            # 권한이 없거나 로그가 아직 없는 경우다. 진단만 로그 없이 진행되고 빌드 결과는 같다.
            logger.warning(
                "build log tail not recorded",
                extra={"action": "record_log_tail", "build_id": build_id},
                exc_info=True,
            )
            return
        async with self._session_factory.begin() as session:
            build = await BuildRepository(session).get_by_id(build_id, for_update=True)
            build.record_log_tail(to_log_tail(tail))

    async def _close(
        self, job: Job, build_id: int, *, cancel: bool = False, image_digest: str | None = None
    ) -> None:
        """빌드를 끝내고 job 을 닫는다. 성공이면 같은 트랜잭션에서 DEPLOY job 을 만든다.

        요청 행을 잠근 뒤 취소 표시를 다시 본다. 빌드 성공과 새 요청이 겹치면 새 요청이 이긴다.
        """
        async with self._session_factory.begin() as session:
            build = await BuildRepository(session).get_by_id(build_id, for_update=True)
            jobs = JobRepository(session)
            if not build.is_finished:
                statuses = DeploymentStatusService.create(session)
                if cancel or build.deployment_request.cancel_requested_at is not None:
                    build.cancel()
                    await statuses.transition_status(
                        build.deployment_request_id, DeploymentStatus.SUPERSEDED
                    )
                elif image_digest is not None:
                    build.succeed(image_digest)
                    await statuses.transition_status(
                        build.deployment_request_id, DeploymentStatus.DEPLOYING
                    )
                    await jobs.save(
                        Job(
                            deployment_request_id=build.deployment_request_id,
                            kind=JobKind.DEPLOY,
                            payload={"build_id": build.id},
                        )
                    )
            await jobs.mark_succeeded(job.id)
        logger.info(
            "build closed",
            extra={"action": "close_build", "build_id": build_id, "build_status": build.status},
        )

    async def _get_build(self, job: Job) -> Build:
        async with self._session_factory.begin() as session:
            return await BuildRepository(session).get_by_id(_build_id(job))


def _build_id(job: Job) -> int:
    return int(job.payload["build_id"])


def _describe(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"[:_MAX_ERROR_LENGTH]


def _normalize_root(root_directory: str | None) -> str:
    return posixpath.normpath(root_directory or ".").strip("/") or "."


def _scan_snapshot(path: Path, root_directory: str | None) -> tuple[set[str], bytes | None]:
    """tarball 을 풀지 않고 root_directory 아래 파일 이름과 iris.json 내용을 읽는다."""
    root = _normalize_root(root_directory)
    prefix = "" if root == "." else f"{root}/"
    file_names: set[str] = set()
    config_content: bytes | None = None
    with tarfile.open(path, "r:gz") as archive:
        for member in archive:
            if not (member.isfile() or member.issym()):
                continue
            # GitHub tarball 은 최상위에 {owner}-{repo}-{sha}/ 디렉터리가 하나 있다.
            relative = member.name.partition("/")[2]
            if not relative.startswith(prefix):
                continue
            name = relative.removeprefix(prefix)
            file_names.add(name)
            if name == CONFIG_FILE_NAME and member.isfile():
                config_file = archive.extractfile(member)
                config_content = config_file.read(_MAX_CONFIG_BYTES) if config_file else None
    return file_names, config_content


@contextlib.contextmanager
def _source_errors(not_found_code: FailureCode) -> Iterator[None]:
    try:
        yield
    except NotFoundError as exc:
        raise BuildFailedError(not_found_code) from exc
    except ForbiddenError as exc:
        raise BuildFailedError(FailureCode.SOURCE_NOT_ACCESSIBLE) from exc
    except SourceTooLargeError as exc:
        raise BuildFailedError(FailureCode.SOURCE_TOO_LARGE) from exc


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> bool:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    return stop.is_set()
