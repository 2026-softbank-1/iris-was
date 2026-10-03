"""DEPLOY·RECONCILE·ROLLBACK·REMOVE job 처리: GitOps 커밋 → Argo CD 상태 확인 → 성공·revert·삭제.

job 은 짧게 끝낸다. Argo CD 반영을 기다릴 때는 job 을 잡고 있지 않고 snooze 한다.
외부에 쓰기 전에 커밋 SHA 를 먼저 기록해, Worker 가 죽어도 중복 커밋 없이 이어서 처리한다.
"""

import contextlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.argocd_client import ArgoAppStatus, ArgoCdClient
from app.clients.aws_clients import EcrClient
from app.clients.github_client import GitHubClient
from app.clients.secret_sealer import SecretSealer
from app.core.config import DeployWorkerSettings
from app.core.crypto import VariableCipher
from app.core.exceptions import (
    ConflictError,
    ExternalError,
    GitOpsConflictError,
    NotConfiguredError,
    NotFoundError,
)
from app.enums import (
    APP_PORT,
    Builder,
    DeploymentStatus,
    DeploymentStrategy,
    Environment,
    FailureCode,
    JobKind,
    ReleaseStatus,
)
from app.models import Job, Release
from app.models.target import AWS_TARGET_NAME
from app.repositories.build_repository import BuildRepository
from app.repositories.deployment_request_repository import DeploymentRequestRepository
from app.repositories.job_repository import JobRepository
from app.repositories.release_repository import ReleaseRepository
from app.services.builder_detection import DeployConfig
from app.services.deployment_status_service import DeploymentStatusService
from app.services.deployment_strategy import PROGRESSIVE_TARGET_KINDS, strategy_extra_wait
from app.services.domain_service import service_host_label
from app.services.scaling_config import ScalingConfig

logger = logging.getLogger(__name__)

JOB_KINDS = frozenset({JobKind.DEPLOY, JobKind.RECONCILE, JobKind.ROLLBACK, JobKind.REMOVE})
GITOPS_BRANCH = "main"
GITOPS_ENVIRONMENT = "prod"
VALUES_FILE_NAME = "values.yaml"
RECONCILE_INTERVAL = timedelta(seconds=10)
IN_FLIGHT_SNOOZE = timedelta(seconds=15)
# 첫 배포의 ApplicationSet 폴링(약 3분)과 Application 폴링(최대 약 3분)을 감안한 여유.
DEADLINE_MARGIN = timedelta(minutes=10)
# 디렉터리를 지운 뒤 ApplicationSet 폴링(약 3분)과 Application 정리를 기다리는 한도.
REMOVE_TIMEOUT = timedelta(minutes=10)
RETRY_BASE_DELAY = timedelta(seconds=30)
MAX_PUSH_ATTEMPTS = 5
_FAILED_PHASES = ("Failed", "Error")
# 설치 토큰은 1시간 유효하다. 만료 직전 토큰을 쓰지 않게 일찍 갱신한다.
_TOKEN_TTL_SECONDS = 50 * 60
_MAX_ERROR_LENGTH = 1000
# iris-service chart 의 values 스키마가 release.sourceSha 에 요구하는 형식.
_GIT_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")


class Verdict(StrEnum):
    WAIT = "WAIT"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"


def evaluate_release(
    status: ArgoAppStatus | None,
    sync_contained: bool,
    operation_contained: bool,
    now: datetime,
    deadline_at: datetime,
) -> Verdict:
    """Argo CD 상태로 release 를 판정한다. *_contained 는 그 revision 이 목표 커밋을 포함하는지다.

    operation 은 목표를 포함할 때만 본다. 직전 release 의 Failed operation 으로 오판하지 않으려고.
    성공은 operation 을 보지 않는다. 목표를 포함한 revision 에서 Synced 면 live 가 목표와 같고,
    Argo 의 Deployment·Rollout Healthy 는 rollout 완료를 뜻한다. 대기는 모두 deadline 에 걸린다.
    카나리·블루그린 Rollout 이 단계 사이에 멈춘 동안의 `Suspended`·`Progressing` 도 대기다.
    """
    if status is not None:
        if operation_contained and status.operation_phase in _FAILED_PHASES:
            return Verdict.FAILED
        # health 는 live 상태다. OutOfSync 면 아직 이전(실패한) release 의 Degraded 일 수 있다.
        is_synced = sync_contained and status.sync_status == "Synced"
        if is_synced and status.health_status == "Healthy":
            return Verdict.SUCCEEDED
        if is_synced and status.health_status == "Degraded":
            return Verdict.FAILED
    if now > deadline_at:
        return Verdict.TIMED_OUT
    return Verdict.WAIT


def render_service_values(
    *,
    host_label: str,
    release_id: int,
    image_repository: str,
    image_digest: str,
    source_sha: str,
    builder: Builder,
    deploy: DeployConfig,
    base_domain: str,
    iris: Mapping[str, Any] | None = None,
    variables: Mapping[str, Any] | None = None,
    scaling: ScalingConfig | None = None,
    deployment_strategy: DeploymentStrategy | None = None,
) -> str:
    """services/{service_id}/{타깃 디렉터리}/values.yaml 내용. iris-service chart 의 values 다.

    배포마다 파일 전체를 새로 만든다. Pod 수와 리소스는 요청 스냅샷을 쓰고, 스냅샷이 없는
    기존 요청과 Ingress·NetworkPolicy는 chart·타겟 기본값을 쓴다. JSON 은 YAML 이다.

    `iris`(서비스·타깃 이름, 배포 요청 id)와 `variables`(봉인한 사용자 변수 `name`·`encryptedData`,
    평문은 받지 않는다)는 chart 0.6.0 부터 받는다. 없으면 쓰지 않아 이전 chart 도 렌더링된다.
    `deployment_strategy` 는 chart 0.7.0 부터 받는다. 없으면 chart 가 ROLLING 으로 렌더링한다.
    """
    health: dict[str, Any] = {"timeoutSeconds": deploy.healthcheck_timeout}
    if deploy.healthcheck_path:
        health["path"] = deploy.healthcheck_path
    # release.id 는 Pod annotation 으로 들어가 digest 가 같아도 release 마다 rollout 된다.
    release: dict[str, Any] = {"id": release_id}
    # chart 스키마가 sourceSha 를 소문자 40자리 Git SHA 로만 받는다. CLI 업로드의 `upload-…`
    # 같은 값을 그대로 넣으면 values 검증에 걸려 Argo CD 가 새 manifest 를 렌더링하지 못하고
    # release 가 PENDING 에서 멈춘다. 그런 소스는 필드를 생략한다(IRIS_GIT_COMMIT_SHA 도 없다).
    if _GIT_SHA_PATTERN.fullmatch(source_sha):
        release["sourceSha"] = source_sha
    values: dict[str, Any] = {
        "image": {"repository": image_repository, "digest": image_digest},
        "release": release,
        "containerPort": APP_PORT,
        "health": health,
        "route": {"host": f"{host_label}.{base_domain}"},
    }
    if iris is not None:
        # 앱에 IRIS_SERVICE_NAME·IRIS_TARGET_NAME·IRIS_DEPLOYMENT_ID 로 주입된다.
        values["iris"] = dict(iris)
    if variables is not None:
        values["variables"] = dict(variables)
    if scaling is not None:
        values.update(scaling.model_dump(mode="json"))
    if deployment_strategy is not None:
        values["deploymentStrategy"] = deployment_strategy.value
    # Railpack 은 빌드 때 start command 를 이미지에 넣는다. Dockerfile 은 ENTRYPOINT·CMD 를
    # exec form 으로 덮어쓴다(셸을 거치지 않아 $VAR 가 풀리지 않는다. 필요하면 sh -c 로 감싼다).
    if builder == Builder.DOCKERFILE and deploy.start_command_args:
        values["command"] = deploy.start_command_args
    return json.dumps(values, indent=2, sort_keys=True) + "\n"


class _RollbackBlockedError(Exception):
    """GitOps HEAD 의 서비스 디렉터리가 실패한 release 커밋과 다르다. 자동 revert 하지 않는다."""


class _AlreadyRemovedError(Exception):
    """GitOps HEAD 에 서비스 디렉터리가 이미 없다. 지울 커밋이 필요 없다."""


class DeployService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        github: GitHubClient,
        argocd: ArgoCdClient,
        ecr: EcrClient,
        settings: DeployWorkerSettings,
        worker_id: str,
        *,
        cipher: VariableCipher | None = None,
        sealer: SecretSealer | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._github = github
        self._argocd = argocd
        self._ecr = ecr
        self._settings = settings
        self._worker_id = worker_id
        # 사용자 변수를 봉인할 때만 쓴다. 변수가 없는 배포는 둘 다 없어도 동작한다.
        self._cipher = cipher
        self._sealer = sealer
        self._token: tuple[str, float] | None = None

    async def claim_next_job(self) -> Job | None:
        async with self._session_factory.begin() as session:
            # user_build_limit 은 BUILD 에만 쓰인다.
            return await JobRepository(session).claim_next_job(self._worker_id, JOB_KINDS, 0)

    async def find_seconds_until_next_run(self) -> float | None:
        async with self._session_factory() as session:
            return await JobRepository(session).find_seconds_until_next_run(JOB_KINDS)

    async def run(self, job: Job) -> None:
        """job 을 한 번 처리한다. 재시도할 만한 실패는 예외로 던진다."""
        if job.attempts > job.max_attempts:
            await self._give_up(job, "attempts exhausted")
            return
        if job.kind == JobKind.DEPLOY:
            await self._deploy(job)
        elif job.kind == JobKind.RECONCILE:
            await self._reconcile(job)
        elif job.kind == JobKind.REMOVE:
            await self._remove(job)
        else:
            await self._rollback(job)

    async def retry_or_fail(self, job: Job, error: Exception) -> None:
        if job.attempts >= job.max_attempts:
            await self._give_up(job, _describe(error))
            return
        async with self._session_factory.begin() as session:
            await JobRepository(session).retry_later(
                job.id, _describe(error), RETRY_BASE_DELAY * 2 ** (job.attempts - 1)
            )

    # --- DEPLOY

    async def _deploy(self, job: Job) -> None:
        async with self._session_factory.begin() as session:
            release = await ReleaseRepository(session).find_by_deployment_request_id(
                job.deployment_request_id
            )
        if release is None:
            release = await self._create_release(job)
            if release is None:
                return
        if release.is_finished:
            async with self._session_factory.begin() as session:
                await JobRepository(session).mark_succeeded(job.id)
            return
        if release.deadline_at is None:
            await self._push_deploy_commit(job, release)
        deploy = DeployConfig.model_validate(release.build.deploy_config or {})
        async with self._session_factory.begin() as session:
            release = await ReleaseRepository(session).get_by_id(release.id, for_update=True)
            if release.deadline_at is None:
                release.confirm_commit(
                    _deadline(deploy, release.deployment_request.deployment_strategy)
                )
                _add_job(session, release, JobKind.RECONCILE)
            await JobRepository(session).mark_succeeded(job.id)
        logger.info(
            "release committed",
            extra={
                "action": "deploy",
                "release_id": release.id,
                "gitops_commit_sha": release.gitops_commit_sha,
            },
        )

    async def _create_release(self, job: Job) -> Release | None:
        """release 를 PENDING 으로 만든다. 대체됐거나 진행 중 release 가 있으면 None."""
        try:
            async with self._session_factory.begin() as session:
                build = await BuildRepository(session).get_by_id(
                    int(job.payload["build_id"]), for_update=True
                )
                request = build.deployment_request
                if request.cancel_requested_at is not None:
                    await _move_request(session, request.id, DeploymentStatus.SUPERSEDED)
                    await JobRepository(session).mark_succeeded(job.id)
                    logger.info("deployment superseded", extra={"action": "deploy"})
                    return None
                releases = ReleaseRepository(session)
                target = await releases.find_deploy_target(request.service_id)
                if target is None:
                    raise NotFoundError("deploy target not found", service_id=request.service_id)
                last_good = await releases.find_last_known_good(request.service_id, target.id)
                assert build.image_digest is not None
                await releases.add(
                    Release(
                        deployment_request_id=request.id,
                        build_id=build.id,
                        service_id=request.service_id,
                        environment=Environment.PROD,
                        target_id=target.id,
                        image_digest=build.image_digest,
                        previous_good_release_id=last_good.id if last_good else None,
                    )
                )
        except ConflictError:
            await self._snooze(job, IN_FLIGHT_SNOOZE)
            logger.info("release in flight, deploy waits", extra={"action": "deploy"})
            return None
        async with self._session_factory.begin() as session:
            return await ReleaseRepository(session).find_by_deployment_request_id(
                job.deployment_request_id
            )

    async def _push_deploy_commit(self, job: Job, release: Release) -> None:
        target = release.target
        if target.domain_suffix is None:
            raise NotConfiguredError("target has no domain suffix", target=target.name)
        token = await self._gitops_token()
        service = release.deployment_request.service
        build = release.build
        assert build.image_repository is not None and build.source_sha is not None
        assert build.builder is not None
        variables = await self._seal_variables(release)
        files = {
            VALUES_FILE_NAME: render_service_values(
                host_label=service_host_label(service.name, service.id),
                release_id=release.id,
                image_repository=build.image_repository,
                image_digest=release.image_digest,
                source_sha=build.source_sha,
                builder=build.builder,
                deploy=DeployConfig.model_validate(build.deploy_config or {}),
                base_domain=target.domain_suffix,
                iris=self._identity(release),
                variables=variables,
                scaling=(
                    ScalingConfig.model_validate(release.deployment_request.scaling_snapshot)
                    if release.deployment_request.scaling_snapshot is not None
                    else None
                ),
                deployment_strategy=self._deployment_strategy(release),
            )
        }

        async def create(head_sha: str) -> str:
            tree_sha = await self._github.create_tree(token, self._repository, files)
            return await self._github.create_commit(
                token,
                self._repository,
                head_sha,
                _service_path(service.id, target.name),
                tree_sha,
                _commit_message(f"deploy service {service.id}", release.id),
            )

        async def record(commit_sha: str) -> None:
            await self._record_commit(job, release.id, commit_sha, is_revert=False)

        await self._push(token, release.gitops_commit_sha, create, record)

    def _identity(self, release: Release) -> dict[str, Any] | None:
        """앱에 알릴 서비스·타깃 이름과 배포 요청 id. 사용자 변수 기능이 켜졌을 때만 쓴다.

        기능은 `SEALED_SECRETS_CERT` 를 설정하면 켜진다. 그 인증서는 controller 가 뜬 클러스터,
        곧 `iris` 와 `variables` 를 받는 chart(0.6.0 이상)가 배포된 뒤에만 있다. 모르는 키는 이전
        chart 의 schema 가 거절하므로, 켜지 않은 Worker 는 이전과 같은 values 를 쓴다.
        """
        if self._sealer is None:
            return None
        return {
            "serviceName": release.deployment_request.service.name,
            "targetName": release.target.name,
            "deploymentId": release.deployment_request_id,
        }

    def _deployment_strategy(self, release: Release) -> DeploymentStrategy | None:
        """요청에 적용한 배포 방식. 기능을 켠 Worker 가 AWS 타깃 release 에만 values 에 쓴다.

        이전 chart(0.7.0 미만)의 schema 는 모르는 키를 거절하므로 켜지 않은 Worker 는 키를 쓰지
        않는다. on-prem 타깃은 chart 0.6.0 에 남아 있어 기능을 켜도 쓰지 않는다. 기능 도입 전
        요청은 방식이 없어 ROLLING 이다.
        """
        if (
            not self._settings.deployment_strategy_enabled
            or release.target.kind not in PROGRESSIVE_TARGET_KINDS
        ):
            return None
        return release.deployment_request.deployment_strategy or DeploymentStrategy.ROLLING

    async def _seal_variables(self, release: Release) -> dict[str, Any] | None:
        """요청 스냅샷의 변수를 풀어 이 release 전용으로 다시 봉인한다. 변수가 없으면 None.

        봉인할 수 없으면 변수를 뺀 채 배포하지 않고 예외로 멈춘다. 앱이 변수 없이 뜨는 것을 막는다.
        """
        snapshot = release.deployment_request.variables_snapshot
        if not snapshot:
            return None
        if self._cipher is None or self._sealer is None:
            raise NotConfiguredError(
                "variables cannot be sealed",
                setting="VARIABLES_ENCRYPTION_KEY, SEALED_SECRETS_CERT",
                release_id=release.id,
            )
        # release 마다 새 이름이라 새 Secret 이 먼저 생기고, 롤백은 이전 이름이 돌아온다.
        name = f"vars-r{release.id}"
        plaintexts = {key: self._cipher.decrypt(token) for key, token in snapshot.items()}
        encrypted = await self._sealer.seal(service_namespace(release.service_id), name, plaintexts)
        return {"name": name, "encryptedData": encrypted}

    # --- RECONCILE

    async def _reconcile(self, job: Job) -> None:
        async with self._session_factory.begin() as session:
            release = await ReleaseRepository(session).get_by_id(_release_id(job))
        if release.is_finished:
            async with self._session_factory.begin() as session:
                await JobRepository(session).mark_succeeded(job.id)
            return
        target = release.target_commit_sha
        assert target is not None and release.deadline_at is not None
        now = datetime.now(UTC)
        observed: tuple[ArgoAppStatus | None, bool, bool]
        try:
            observed = await self._observe(release.service_id, target)
        except ExternalError:
            if now <= release.deadline_at:
                logger.warning(
                    "release status check failed", exc_info=True, extra={"action": "reconcile"}
                )
                await self._snooze(job, RECONCILE_INTERVAL)
                return
            observed = (None, False, False)
        verdict = evaluate_release(*observed, now=now, deadline_at=release.deadline_at)
        if verdict == Verdict.WAIT:
            await self._snooze(job, RECONCILE_INTERVAL)
        elif verdict == Verdict.SUCCEEDED:
            await self._succeed(job, release)
        else:
            failure_code = (
                FailureCode.DEPLOY_TIMED_OUT
                if verdict == Verdict.TIMED_OUT
                else FailureCode.DEPLOY_FAILED
            )
            await self._fail(job, release, failure_code, observed[0])

    async def _observe(
        self, service_id: int, target_sha: str
    ) -> tuple[ArgoAppStatus | None, bool, bool]:
        """Argo 가 아직 목표 커밋을 못 봤으면 refresh 해서 한 번 더 읽는다."""
        name = _argo_application_name(service_id)
        status = await self._argocd.get_application(name)
        if status is None:
            return None, False, False
        sync_contained = await self._contains(target_sha, status.sync_revision)
        if not sync_contained:
            status = await self._argocd.get_application(name, refresh=True)
            if status is None:
                return None, False, False
            sync_contained = await self._contains(target_sha, status.sync_revision)
        operation_contained = status.operation_phase in _FAILED_PHASES and await self._contains(
            target_sha, status.operation_revision
        )
        return status, sync_contained, operation_contained

    async def _succeed(self, job: Job, release: Release) -> None:
        if release.status == ReleaseStatus.PENDING:
            # 배포된 이미지가 ECR lifecycle 에 지워지지 않게 r-* 태그로 지킨다.
            # 태그는 보호장치일 뿐이라 실패해도 배포 성공을 막지 않는다.
            # ponytail: 실패한 태그는 다시 붙이지 않는다. 같은 서비스에 b-* 빌드가 10개 넘게
            #   쌓이면 이미지가 지워질 수 있다. 겪으면 태그 누락분을 다시 태그하는 job 을 둔다.
            assert release.build.image_repository is not None
            try:
                await self._ecr.tag_image(
                    release.build.image_repository.split("/", 1)[1],
                    release.image_digest,
                    f"r-{release.id}",
                )
            except ExternalError:
                logger.warning(
                    "release image tag failed",
                    exc_info=True,
                    extra={"action": "reconcile", "release_id": release.id},
                )
        async with self._session_factory.begin() as session:
            release = await ReleaseRepository(session).get_by_id(release.id, for_update=True)
            if release.status == ReleaseStatus.ROLLING_BACK:
                release.roll_back()
                await _move_request(
                    session, release.deployment_request_id, DeploymentStatus.ROLLED_BACK
                )
            else:
                release.succeed()
                await _move_request(
                    session, release.deployment_request_id, DeploymentStatus.SUCCEEDED
                )
            await JobRepository(session).mark_succeeded(job.id)
        logger.info(
            "release finished",
            extra={
                "action": "reconcile",
                "release_id": release.id,
                "release_status": release.status,
            },
        )

    async def _fail(
        self,
        job: Job,
        release: Release,
        failure_code: FailureCode,
        status: ArgoAppStatus | None,
    ) -> None:
        """rollback 중 실패면 운영자에게, 이전 정상 release 가 있으면 ROLLBACK 으로 넘긴다."""
        needs_manual = False
        async with self._session_factory.begin() as session:
            release = await ReleaseRepository(session).get_by_id(release.id, for_update=True)
            if release.status == ReleaseStatus.ROLLING_BACK:
                release.fail(release.failure_code or failure_code)
                await _move_request(
                    session, release.deployment_request_id, DeploymentStatus.MANUAL_INTERVENTION
                )
                needs_manual = True
            elif release.previous_good_release_id is not None:
                # 요청은 FAILED 로 두고, 되돌림이 끝나면 ROLLED_BACK 으로 옮긴다. 되돌리는 동안
                # 새 배포가 끼어들지 못하게 하는 것은 release 의 진행 중 index 다.
                release.record_failure(failure_code)
                await _move_request(
                    session,
                    release.deployment_request_id,
                    DeploymentStatus.FAILED,
                    failure_code,
                )
                _add_job(session, release, JobKind.ROLLBACK)
            else:
                # 첫 배포는 되돌릴 곳이 없다. Git·Pod 를 그대로 두고 다음 배포가 덮어쓴다.
                release.fail(failure_code)
                await _move_request(
                    session,
                    release.deployment_request_id,
                    DeploymentStatus.FAILED,
                    failure_code,
                )
            await JobRepository(session).mark_succeeded(job.id)
        extra = {
            "action": "reconcile",
            "release_id": release.id,
            "failure_code": failure_code,
            "argo_sync_status": status and status.sync_status,
            "argo_health_status": status and status.health_status,
            "argo_operation_message": status and status.operation_message,
        }
        if needs_manual:
            logger.error("rollback failed", extra={**extra, "action": "rollback_blocked"})
        else:
            logger.info("release failed", extra=extra)

    # --- ROLLBACK

    async def _rollback(self, job: Job) -> None:
        async with self._session_factory.begin() as session:
            releases = ReleaseRepository(session)
            release = await releases.get_by_id(_release_id(job))
            assert release.previous_good_release_id is not None
            previous = await releases.get_by_id(release.previous_good_release_id)
        if release.status == ReleaseStatus.PENDING:
            try:
                await self._push_revert_commit(job, release, previous)
            except _RollbackBlockedError:
                await self._block_rollback(job, release)
                return
        deploy = DeployConfig.model_validate(previous.build.deploy_config or {})
        async with self._session_factory.begin() as session:
            release = await ReleaseRepository(session).get_by_id(release.id, for_update=True)
            if release.status == ReleaseStatus.PENDING:
                # revert 는 이전 정상 release 의 values 로 돌아가므로 그 방식으로 교체된다.
                release.start_rollback(
                    _deadline(deploy, previous.deployment_request.deployment_strategy)
                )
                _add_job(session, release, JobKind.RECONCILE)
            await JobRepository(session).mark_succeeded(job.id)
        logger.info(
            "release revert committed",
            extra={
                "action": "rollback",
                "release_id": release.id,
                "revert_commit_sha": release.revert_commit_sha,
            },
        )

    async def _push_revert_commit(self, job: Job, release: Release, previous: Release) -> None:
        """services/{id} 를 이전 정상 release 커밋의 디렉터리로 되돌린다.

        HEAD 의 디렉터리가 실패한 release 커밋과 같을 때만 한다. 더 최신 배포가 없다는 조건은
        이 release 가 진행 중인 동안 in-flight index 가 보장한다.
        """
        assert release.gitops_commit_sha is not None and previous.gitops_commit_sha is not None
        token = await self._gitops_token()
        path = _service_path(release.service_id, release.target.name)
        failed_sha, good_sha = release.gitops_commit_sha, previous.gitops_commit_sha

        async def create(head_sha: str) -> str:
            find = self._github.find_subtree_sha
            head_tree = await find(token, self._repository, head_sha, path)
            if head_tree != await find(token, self._repository, failed_sha, path):
                raise _RollbackBlockedError
            good_tree = await find(token, self._repository, good_sha, path)
            if good_tree is None:
                raise _RollbackBlockedError
            return await self._github.create_commit(
                token,
                self._repository,
                head_sha,
                path,
                good_tree,
                _commit_message(f"revert service {release.service_id}", release.id),
            )

        async def record(commit_sha: str) -> None:
            await self._record_commit(job, release.id, commit_sha, is_revert=True)

        await self._push(token, release.revert_commit_sha, create, record)

    async def _block_rollback(self, job: Job, release: Release) -> None:
        async with self._session_factory.begin() as session:
            release = await ReleaseRepository(session).get_by_id(release.id, for_update=True)
            release.fail(release.failure_code or FailureCode.DEPLOY_FAILED)
            await _move_request(
                session, release.deployment_request_id, DeploymentStatus.MANUAL_INTERVENTION
            )
            await JobRepository(session).mark_manual_intervention(job.id, "gitops head changed")
        logger.error(
            "gitops head changed, rollback skipped",
            extra={"action": "rollback_blocked", "release_id": release.id},
        )

    # --- REMOVE

    async def _remove(self, job: Job) -> None:
        """GitOps 에서 서비스 디렉터리를 지우고 Argo CD Application 이 사라질 때까지 기다린다.

        커밋 SHA 를 먼저 기록해 Worker 가 죽어도 중복 커밋 없이 이어서 처리한다. Application 은
        ApplicationSet 이 디렉터리 삭제를 보고 지운다. 기한 안에 사라지지 않으면 운영자에게 넘긴다.
        """
        async with self._session_factory.begin() as session:
            request = await DeploymentRequestRepository(session).get_by_id(
                job.deployment_request_id
            )
            target = await ReleaseRepository(session).find_deploy_target(request.service_id)
        if target is None:
            raise NotFoundError("deploy target not found", service_id=request.service_id)
        if request.status != DeploymentStatus.DEPLOYING:
            async with self._session_factory.begin() as session:
                await JobRepository(session).mark_succeeded(job.id)
            return

        await self._delete_service_directory(job, request.service_id, target.name)

        now = datetime.now(UTC)
        is_expired = now > job.created_at + REMOVE_TIMEOUT
        try:
            application = await self._argocd.get_application(
                _argo_application_name(request.service_id)
            )
        except ExternalError:
            if is_expired:
                await self._block_remove(job, "argocd unreachable after remove commit")
                return
            logger.warning("application check failed", exc_info=True, extra={"action": "remove"})
            await self._snooze(job, RECONCILE_INTERVAL)
            return
        if application is None:
            async with self._session_factory.begin() as session:
                await _move_request(session, request.id, DeploymentStatus.SUCCEEDED)
                await JobRepository(session).mark_succeeded(job.id)
            logger.info(
                "service removed", extra={"action": "remove", "service_id": request.service_id}
            )
        elif is_expired:
            await self._block_remove(job, "application still present after remove commit")
        else:
            await self._snooze(job, RECONCILE_INTERVAL)

    async def _delete_service_directory(self, job: Job, service_id: int, target_name: str) -> None:
        """services/{id}/{타깃 디렉터리} 를 지우는 커밋을 main 에 올린다. 없으면 건너뛴다."""
        token = await self._gitops_token()
        path = _service_path(service_id, target_name)

        async def create(head_sha: str) -> str:
            subtree_sha = await self._github.find_subtree_sha(
                token, self._repository, head_sha, path
            )
            if subtree_sha is None:
                raise _AlreadyRemovedError
            return await self._github.create_delete_commit(
                token,
                self._repository,
                head_sha,
                path,
                _remove_commit_message(service_id, job.deployment_request_id),
            )

        async def record(commit_sha: str) -> None:
            async with self._session_factory.begin() as session:
                await JobRepository(session).record_external_id(job.id, commit_sha)

        with contextlib.suppress(_AlreadyRemovedError):
            await self._push(token, job.external_id, create, record)

    async def _block_remove(self, job: Job, error: str) -> None:
        """GitOps 는 이미 바뀌었는데 서비스가 내려갔는지 알 수 없다. 운영자에게 넘긴다."""
        async with self._session_factory.begin() as session:
            await _move_request(
                session, job.deployment_request_id, DeploymentStatus.MANUAL_INTERVENTION
            )
            await JobRepository(session).mark_manual_intervention(job.id, error)
        logger.error("remove blocked", extra={"action": "remove_blocked", "reason": error})

    async def _give_up_remove(self, job: Job, error: str) -> None:
        """커밋 전이면 서비스가 그대로라 FAILED, 커밋 후면 상태를 알 수 없어 운영자에게 넘긴다."""
        async with self._session_factory.begin() as session:
            stored = await session.get_one(Job, job.id)
            request = await DeploymentRequestRepository(session).get_by_id(
                job.deployment_request_id
            )
            is_committed = stored.external_id is not None
            if request.status == DeploymentStatus.DEPLOYING:
                if is_committed:
                    await _move_request(session, request.id, DeploymentStatus.MANUAL_INTERVENTION)
                else:
                    await _move_request(
                        session,
                        request.id,
                        DeploymentStatus.FAILED,
                        FailureCode.DEPLOY_INFRA_ERROR,
                    )
            if is_committed:
                await JobRepository(session).mark_manual_intervention(job.id, error)
            else:
                await JobRepository(session).mark_failed(job.id, error)
        if is_committed:
            logger.error("remove blocked", extra={"action": "remove_blocked", "reason": error})

    # --- 공용

    async def _push(
        self,
        token: str,
        recorded_sha: str | None,
        create: Callable[[str], Awaitable[str]],
        record: Callable[[str], Awaitable[None]],
    ) -> None:
        """커밋을 main 에 fast-forward 한다. 기록된 커밋이 이미 main 에 있으면 그대로 끝낸다.

        브랜치가 그새 움직였으면 새 HEAD 위에 커밋을 다시 만든다.
        """
        commit_sha = recorded_sha
        if commit_sha is not None:
            head_sha = await self._github.get_branch_sha(token, self._repository, GITOPS_BRANCH)
            if await self._github.contains(token, self._repository, commit_sha, head_sha):
                return
        for _ in range(MAX_PUSH_ATTEMPTS):
            if commit_sha is None:
                head_sha = await self._github.get_branch_sha(token, self._repository, GITOPS_BRANCH)
                commit_sha = await create(head_sha)
                await record(commit_sha)
            try:
                await self._github.update_branch(token, self._repository, GITOPS_BRANCH, commit_sha)
                return
            except GitOpsConflictError:
                logger.info("gitops branch moved, recommitting", extra={"action": "push"})
                commit_sha = None
        raise ExternalError("gitops branch kept moving", attempts=MAX_PUSH_ATTEMPTS)

    async def _record_commit(
        self, job: Job, release_id: int, commit_sha: str, *, is_revert: bool
    ) -> None:
        async with self._session_factory.begin() as session:
            release = await ReleaseRepository(session).get_by_id(release_id, for_update=True)
            if is_revert:
                release.record_revert(commit_sha)
            else:
                release.record_commit(commit_sha)
            await JobRepository(session).record_external_id(job.id, commit_sha)

    async def _contains(self, target_sha: str, revision: str | None) -> bool:
        if not revision:
            return False
        token = await self._gitops_token()
        return await self._github.contains(token, self._repository, target_sha, revision)

    async def _give_up(self, job: Job, error: str) -> None:
        """job 을 FAILED 로 닫고 release·요청도 같은 트랜잭션에서 닫는다.

        커밋 전이면 Git 이 바뀌지 않았으니 FAILED, 커밋 후면 상태를 알 수 없어 운영자에게 넘긴다.
        """
        if job.kind == JobKind.REMOVE:
            await self._give_up_remove(job, error)
            return
        needs_manual = False
        async with self._session_factory.begin() as session:
            release = await ReleaseRepository(session).find_by_deployment_request_id(
                job.deployment_request_id
            )
            if release is None:
                build = await BuildRepository(session).get_by_id(
                    int(job.payload["build_id"]), for_update=True
                )
                await _move_request(
                    session,
                    build.deployment_request_id,
                    DeploymentStatus.FAILED,
                    FailureCode.DEPLOY_INFRA_ERROR,
                )
            elif not release.is_finished:
                release = await ReleaseRepository(session).get_by_id(release.id, for_update=True)
                failure_code = release.failure_code or FailureCode.DEPLOY_INFRA_ERROR
                release.fail(failure_code)
                if release.gitops_commit_sha is None:
                    await _move_request(
                        session,
                        release.deployment_request_id,
                        DeploymentStatus.FAILED,
                        failure_code,
                    )
                else:
                    await _move_request(
                        session, release.deployment_request_id, DeploymentStatus.MANUAL_INTERVENTION
                    )
                    needs_manual = True
            await JobRepository(session).mark_failed(job.id, error)
        if needs_manual:
            logger.error(
                "job gave up after gitops commit",
                extra={"action": "rollback_blocked", "release_id": release and release.id},
            )

    async def _snooze(self, job: Job, delay: timedelta) -> None:
        async with self._session_factory.begin() as session:
            await JobRepository(session).release(job.id, delay)

    async def _gitops_token(self) -> str:
        if self._token is None or time.monotonic() >= self._token[1]:
            token = await self._github.create_installation_token(
                self._settings.gitops_installation_id, None, contents="write"
            )
            self._token = (token, time.monotonic() + _TOKEN_TTL_SECONDS)
        return self._token[0]

    @property
    def _repository(self) -> str:
        return self._settings.gitops_repository


async def _move_request(
    session: AsyncSession,
    deployment_request_id: int,
    to_status: DeploymentStatus,
    failure_code: FailureCode | None = None,
) -> None:
    """요청 상태는 DeploymentStatusService 로만 바꾼다. 같은 트랜잭션에서 이력도 남긴다."""
    await DeploymentStatusService.create(session).transition_status(
        deployment_request_id, to_status, failure_code=failure_code
    )


def _add_job(session: AsyncSession, release: Release, kind: JobKind) -> None:
    session.add(
        Job(
            deployment_request_id=release.deployment_request_id,
            kind=kind,
            payload={"release_id": release.id},
        )
    )


def _deadline(deploy: DeployConfig, strategy: DeploymentStrategy | None) -> datetime:
    """Argo CD 반영·정상화 기한. 카나리·블루그린은 chart 의 고정 대기 시간만큼 더 기다린다."""
    return (
        datetime.now(UTC)
        + timedelta(seconds=deploy.healthcheck_timeout)
        + DEADLINE_MARGIN
        + strategy_extra_wait(strategy)
    )


def _argo_application_name(service_id: int) -> str:
    return f"svc-{service_id}"


def service_namespace(service_id: int) -> str:
    """사용자 서비스 namespace. iris-infra ApplicationSet 의 `svc-{id}` 와 같아야 풀린다."""
    return f"svc-{service_id}"


def _service_path(service_id: int, target_name: str) -> str:
    """Deploy Worker 가 통째로 쓰는 디렉터리. 이 디렉터리마다 Application 이 생긴다.

    `aws` 는 타깃 도입 전 경로(prod)를 쓴다. 옮기면 Argo 가 svc-{id} 를 지웠다 다시 만든다.
    """
    directory = GITOPS_ENVIRONMENT if target_name == AWS_TARGET_NAME else target_name
    return f"services/{service_id}/{directory}"


def _commit_message(subject: str, release_id: int) -> str:
    return f"{subject}\n\nIris-Release-Id: {release_id}\n"


def _remove_commit_message(service_id: int, deployment_request_id: int) -> str:
    return f"remove service {service_id}\n\nIris-Deployment-Request-Id: {deployment_request_id}\n"


def _release_id(job: Job) -> int:
    return int(job.payload["release_id"])


def _describe(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"[:_MAX_ERROR_LENGTH]
