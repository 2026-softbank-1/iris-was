"""Apply immutable image digests through GitOps, then verify the corresponding Argo revision."""

import asyncio
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.argocd_client import ArgoAppStatus, ArgoCdClient
from app.clients.aws_clients import EcrClient
from app.clients.failure_log_client import mask_failure_log
from app.clients.gitops_client import GithubGitOpsClient
from app.core.deploy_config import DeployWorkerSettings
from app.core.exceptions import AppError, ExternalError, InvalidInputError, NotConfiguredError
from app.core.worker_exceptions import GitOpsConflictError, JobLeaseLostError
from app.enums import DeploymentStatus, FailureCode, JobKind, ReleaseStatus
from app.models.job import Job
from app.models.pipeline_run import PipelineRun
from app.models.release import Release
from app.repositories.build_repository import BuildRepository
from app.repositories.deployment_request_repository import DeploymentRequestRepository
from app.repositories.deployment_status_history_repository import DeploymentStatusHistoryRepository
from app.repositories.job_repository import JobRepository
from app.repositories.release_repository import ReleaseRepository
from app.schemas.build_worker import BuildConfig, BuildJobPayload, validate_relative_path
from app.schemas.pipeline import PipelineVariable
from app.services.deployment_status_service import DeploymentStatusService
from app.services.pipeline_contract import digest_pipeline_plan, validate_pipeline_plan

logger = logging.getLogger(__name__)
JOB_KINDS = frozenset({JobKind.DEPLOY, JobKind.RECONCILE, JobKind.ROLLBACK})


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
    if status is not None:
        if operation_contained and status.operation_phase in {"Failed", "Error"}:
            return Verdict.FAILED
        if sync_contained and status.sync_status == "Synced":
            if status.health_status == "Healthy":
                return Verdict.SUCCEEDED
            if status.health_status == "Degraded":
                return Verdict.FAILED
    return Verdict.TIMED_OUT if now > deadline_at else Verdict.WAIT


def render_service_values(
    *,
    service_id: int,
    target_id: int,
    release_id: int,
    image_repository: str,
    image_digest: str,
    source_sha: str,
    config: BuildConfig,
    domain_suffix: str,
) -> str:
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_digest):
        raise InvalidInputError("an immutable image digest is required")
    environment: list[dict[str, Any]] = []
    for item in config.runtime_env:
        binding = PipelineVariable.model_validate(item)
        if binding.key == "PORT":
            if binding.value != str(config.port):
                raise InvalidInputError("PORT binding differs from the confirmed container port")
            continue
        if binding.key.startswith("IRIS_"):
            raise InvalidInputError("runtime variables cannot override platform metadata")
        if binding.value is not None:
            environment.append({"name": binding.key, "value": binding.value})
        else:
            environment.append(
                {
                    "name": binding.key,
                    "valueFrom": {
                        "secretKeyRef": {"name": binding.secret_ref, "key": binding.secret_key}
                    },
                }
            )
    values: dict[str, Any] = {
        "image": {"repository": image_repository, "digest": image_digest},
        "release": {"id": release_id, "sourceSha": source_sha},
        "containerPort": config.port,
        "route": {"host": f"iris-{service_id}.{domain_suffix}"},
        "health": {"timeoutSeconds": config.healthcheck_timeout},
        "environment": environment,
    }
    if config.healthcheck_path:
        values["health"]["path"] = config.healthcheck_path
    # User-confirmed shell commands retain their semantics; the API never executes them.
    if config.start_command:
        values["command"] = ["/bin/sh", "-c", config.start_command]
    return json.dumps(values, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


class DeployService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        gitops: GithubGitOpsClient,
        argocd: ArgoCdClient,
        settings: DeployWorkerSettings,
        worker_id: str,
        ecr: EcrClient,
    ) -> None:
        self._sessions = sessions
        self._gitops = gitops
        self._argocd = argocd
        self._settings = settings
        self._worker_id = worker_id
        self._ecr = ecr

    async def claim_next_job(self) -> Job | None:
        async with self._sessions.begin() as session:
            return await JobRepository(session).claim_next_job(
                self._worker_id, JOB_KINDS, self._settings.lease_seconds
            )

    async def process_job(self, job: Job) -> None:
        task = asyncio.create_task(self.run(job))
        heartbeat = asyncio.create_task(self._heartbeat(job, task))
        try:
            await task
        except JobLeaseLostError:
            logger.warning("job lease lost", extra={"action": "process_deploy"})
        except asyncio.CancelledError:
            await asyncio.gather(task, return_exceptions=True)
            await self._release(job)
            raise
        except AppError as error:
            if error.retryable:
                await self.retry_or_fail(job, error.code)
            else:
                await self._give_up(job, error.code)
        except (ValidationError, KeyError, ValueError, TypeError):
            await self._give_up(job, "DEPLOY_CONFIG_INVALID")
        except Exception:
            logger.error("deployment execution failed", extra={"action": "process_deploy"})
            await self.retry_or_fail(job, "DEPLOY_WORKER_ERROR")
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def run(self, job: Job) -> None:
        if job.attempts > job.max_attempts:
            await self._give_up(job, "DEPLOY_ATTEMPTS_EXHAUSTED")
            return
        if job.kind == JobKind.DEPLOY:
            await self._deploy(job)
        elif job.kind == JobKind.RECONCILE:
            await self._reconcile(job)
        elif job.kind == JobKind.ROLLBACK:
            await self._rollback(job)

    async def _deploy(self, job: Job) -> None:
        payload = BuildJobPayload.model_validate(job.payload)
        async with self._sessions.begin() as session:
            await JobRepository(session).get_owned_job(job, self._worker_id)
            request = await DeploymentRequestRepository(session).get_by_id_for_update(
                job.deployment_request_id
            )
            if request.status != DeploymentStatus.DEPLOYING:
                await JobRepository(session).mark_succeeded(job, self._worker_id)
                return
            run = await session.get(PipelineRun, job.payload.get("pipeline_run_id"))
            if run is None or run.execution_plan is None:
                raise InvalidInputError("deployment requires an immutable pipeline plan")
            validate_pipeline_plan(run.execution_plan)
            plan = run.execution_plan
            if (
                len(plan["targetIds"]) > 1
                and "{target_id}" not in self._settings.gitops_path_template
            ):
                raise InvalidInputError(
                    "the configured ApplicationSet supports one target per service"
                )
            if (
                digest_pipeline_plan(plan) != job.payload.get("analysis_plan_digest")
                or run.plan_digest != job.payload.get("analysis_plan_digest")
                or plan["sourceSha"] != payload.source_sha
                or plan["sourceRepositoryUrl"] != payload.source_repository_url
                or plan["targetIds"] != payload.target_ids
                or plan["serviceId"] != request.service_id
                or request.source_sha != payload.source_sha
                or payload.build_config.model_copy(
                    update={"analysis_plan_digest": None}
                ).model_dump(mode="json")
                != BuildConfig.model_validate(plan["buildConfig"]).model_dump(mode="json")
            ):
                raise InvalidInputError("deployment plan provenance does not match the queue")
            build = await BuildRepository(session).get_by_id(int(job.payload["build_id"]))
            if (
                build.deployment_request_id != request.id
                or build.source_sha != payload.source_sha
                or build.image_digest is None
                or build.image_repository is None
                or build.build_config != payload.build_config.model_dump(mode="json")
            ):
                raise InvalidInputError("deployment image is not the verified build output")
            releases = await ReleaseRepository(session).search_by_deployment_request_id(request.id)
            existing_targets = {release.target_id for release in releases}
            for target_id in plan["targetIds"]:
                if target_id in existing_targets:
                    continue
                previous = await ReleaseRepository(session).find_last_known_good(
                    request.service_id, request.environment, target_id
                )
                releases.append(
                    await ReleaseRepository(session).save(
                        Release(
                            deployment_request_id=request.id,
                            service_id=request.service_id,
                            environment=request.environment,
                            target_id=target_id,
                            image_digest=build.image_digest,
                            image_repository=build.image_repository,
                            previous_good_release_id=previous.id if previous is not None else None,
                            status=ReleaseStatus.PENDING,
                            deadline_at=datetime.now(UTC)
                            + timedelta(seconds=payload.build_config.healthcheck_timeout + 600),
                        )
                    )
                )
            targets = {target["id"]: target for target in plan["targetBindings"]}
        for release in releases:
            target = targets[release.target_id]
            if target.get("kind") != "AWS":
                raise InvalidInputError("this worker supports AWS GitOps targets")
            await self._check_owned(job)
            assert release.image_repository is not None
            # Lifecycle policy protects r-* tags; a deployed image must survive build-tag cleanup.
            await self._ecr.tag_image(
                release.image_repository.split("/", 1)[1],
                release.image_digest,
                f"r-{release.id}",
            )
            values = render_service_values(
                service_id=release.service_id,
                target_id=release.target_id,
                release_id=release.id,
                image_repository=str(release.image_repository),
                image_digest=release.image_digest,
                source_sha=payload.source_sha,
                config=payload.build_config,
                domain_suffix=target.get("domainSuffix") or self._settings.base_domain,
            )
            await self._push_release(job, release, values)
        async with self._sessions.begin() as session:
            jobs = JobRepository(session)
            await jobs.get_owned_job(job, self._worker_id)
            await jobs.save(
                Job(
                    deployment_request_id=job.deployment_request_id,
                    kind=JobKind.RECONCILE,
                    payload={"release_ids": [release.id for release in releases]},
                )
            )
            await jobs.mark_succeeded(job, self._worker_id)

    async def _push_release(self, job: Job, release: Release, values: str) -> None:
        commit = release.gitops_commit_sha
        for _ in range(5):
            await self._check_owned(job)
            head = await self._gitops.get_head_sha()
            if commit is not None and await self._gitops.contains(commit, head):
                return
            if commit is None:
                commit = await self._gitops.create_values_commit(
                    head,
                    self._path(release),
                    values,
                    f"deploy service {release.service_id}\n\nIris-Release-Id: {release.id}\n",
                )
                # The commit object has no effect until push; persist before updating the ref.
                await self._record_commit(job, release.id, commit)
            await self._check_owned(job)
            try:
                await self._gitops.update_branch(commit)
                return
            except GitOpsConflictError:
                commit = None
        raise ExternalError("gitops branch kept moving")

    async def _record_commit(
        self, job: Job, release_id: int, commit: str, *, is_revert: bool = False
    ) -> None:
        async with self._sessions.begin() as session:
            jobs = JobRepository(session)
            await jobs.get_owned_job(job, self._worker_id)
            current = await ReleaseRepository(session).get_by_id(release_id, for_update=True)
            if is_revert:
                current.revert_commit_sha = commit
                current.deadline_at = datetime.now(UTC) + timedelta(minutes=15)
            else:
                current.gitops_commit_sha = commit
            await jobs.record_external_id(job, self._worker_id, commit)

    async def _reconcile(self, job: Job) -> None:
        async with self._sessions.begin() as session:
            await JobRepository(session).get_owned_job(job, self._worker_id)
            releases = [
                await ReleaseRepository(session).get_by_id(int(release_id))
                for release_id in job.payload["release_ids"]
            ]
        needs_wait = False
        for release in releases:
            if release.status != ReleaseStatus.PENDING:
                continue
            if release.gitops_commit_sha is None or release.deadline_at is None:
                raise InvalidInputError("release is missing its GitOps commit or deadline")
            application = self._application(release)
            status = await self._argocd.get_application(application, refresh=True)
            sync_contained = status is not None and await self._gitops.contains(
                release.gitops_commit_sha, status.sync_revision
            )
            operation_contained = status is not None and await self._gitops.contains(
                release.gitops_commit_sha, status.operation_revision
            )
            verdict = evaluate_release(
                status, sync_contained, operation_contained, datetime.now(UTC), release.deadline_at
            )
            if verdict == Verdict.WAIT:
                needs_wait = True
            async with self._sessions.begin() as session:
                jobs = JobRepository(session)
                current_job = await jobs.get_owned_job(job, self._worker_id)
                current = await ReleaseRepository(session).get_by_id(release.id, for_update=True)
                if status is not None:
                    current.argo_sync_status = status.sync_status
                    current.argo_health_status = status.health_status
                if verdict == Verdict.SUCCEEDED:
                    current.status = ReleaseStatus.SUCCEEDED
                elif verdict in {Verdict.FAILED, Verdict.TIMED_OUT}:
                    current.status = ReleaseStatus.FAILED
                    request = await DeploymentRequestRepository(session).get_by_id_for_update(
                        job.deployment_request_id
                    )
                    # Only a message from the target operation is evidence for this deployment.
                    if operation_contained and status is not None and status.operation_message:
                        secrets = [
                            str(value)
                            for value in (request.variables_snapshot or {}).values()
                            if isinstance(value, (str, int, float)) and len(str(value)) >= 3
                        ]
                        try:
                            message = mask_failure_log(status.operation_message, secrets)[:32000]
                        except NotConfiguredError:
                            message = ""
                        current_job.payload = {
                            **current_job.payload,
                            "failureLog": {
                                "sourceId": f"argo:{application}",
                                "stage": "deploy",
                                "text": message,
                                "sourceLineStart": 1,
                                "isComplete": False,
                                "artifactRef": release.gitops_commit_sha,
                            },
                            "failureLogLimitations": [
                                "Argo operation message only; runtime pod logs unavailable"
                                if message
                                else "Argo message omitted because redaction package is unavailable"
                            ],
                        }
                    await jobs.save(
                        Job(
                            deployment_request_id=job.deployment_request_id,
                            kind=JobKind.ROLLBACK,
                            payload={"release_id": release.id},
                        )
                    )
                    if request.status == DeploymentStatus.DEPLOYING:
                        await _statuses(session).transition_status(
                            request.id,
                            DeploymentStatus.FAILED,
                            failure_code=FailureCode.DEPLOY_FAILED,
                        )
        if needs_wait:
            await self._release(job, timedelta(seconds=self._settings.reconcile_interval_seconds))
            return
        async with self._sessions.begin() as session:
            jobs = JobRepository(session)
            await jobs.get_owned_job(job, self._worker_id)
            releases = await ReleaseRepository(session).search_by_deployment_request_id(
                job.deployment_request_id
            )
            request = await DeploymentRequestRepository(session).get_by_id_for_update(
                job.deployment_request_id
            )
            if releases and all(release.status == ReleaseStatus.SUCCEEDED for release in releases):
                if request.status == DeploymentStatus.DEPLOYING:
                    await _statuses(session).transition_status(
                        request.id, DeploymentStatus.SUCCEEDED
                    )
            await jobs.mark_succeeded(job, self._worker_id)

    async def _rollback(self, job: Job) -> None:
        async with self._sessions.begin() as session:
            await JobRepository(session).get_owned_job(job, self._worker_id)
            repository = ReleaseRepository(session)
            release = await repository.get_by_id(int(job.payload["release_id"]))
            previous = (
                await repository.get_by_id(release.previous_good_release_id)
                if release.previous_good_release_id is not None
                else None
            )
            latest_good = await repository.find_last_known_good(
                release.service_id, release.environment, release.target_id
            )
            blocked = (
                previous is None
                or latest_good is None
                or latest_good.id != previous.id
                or previous.gitops_commit_sha is None
                or release.gitops_commit_sha is None
                or await repository.check_newer_active_deployment(release)
            )
        if release.status == ReleaseStatus.ROLLED_BACK:
            async with self._sessions.begin() as session:
                await JobRepository(session).mark_succeeded(job, self._worker_id)
            return
        if blocked or previous is None:
            await self._block_rollback(job)
            return
        assert previous.gitops_commit_sha is not None and release.gitops_commit_sha is not None
        commit = release.revert_commit_sha
        for _ in range(5):
            await self._check_owned(job)
            head = await self._gitops.get_head_sha()
            if commit is not None and await self._gitops.contains(commit, head):
                break
            # Check both digest and subtree equality. Another deploy may update env/config while
            # retaining the same image; reverting such a newer manifest is not safe.
            values = await self._gitops.find_values(head, self._path(release))
            current_tree = await self._gitops.find_subtree_sha(head, self._path(release))
            failed_tree = await self._gitops.find_subtree_sha(
                release.gitops_commit_sha, self._path(release)
            )
            if (
                values is None
                or values.get("image", {}).get("digest") != release.image_digest
                or current_tree != failed_tree
            ):
                await self._block_rollback(job)
                return
            if commit is None:
                good_tree = await self._gitops.find_subtree_sha(
                    previous.gitops_commit_sha, self._path(release)
                )
                if good_tree is None:
                    await self._block_rollback(job)
                    return
                commit = await self._gitops.create_subtree_commit(
                    head,
                    self._path(release),
                    good_tree,
                    f"revert service {release.service_id}\n\nIris-Release-Id: {release.id}\n",
                )
                await self._record_commit(job, release.id, commit, is_revert=True)
            await self._check_owned(job)
            try:
                await self._gitops.update_branch(commit)
                break
            except GitOpsConflictError:
                commit = None
        else:
            raise ExternalError("gitops branch kept moving during rollback")
        assert commit is not None
        status = await self._argocd.get_application(self._application(release), refresh=True)
        sync_contained = status is not None and await self._gitops.contains(
            commit, status.sync_revision
        )
        operation_contained = status is not None and await self._gitops.contains(
            commit, status.operation_revision
        )
        async with self._sessions.begin() as session:
            await JobRepository(session).get_owned_job(job, self._worker_id)
            current = await ReleaseRepository(session).get_by_id(release.id)
            deadline = current.deadline_at
        assert deadline is not None
        verdict = evaluate_release(
            status, sync_contained, operation_contained, datetime.now(UTC), deadline
        )
        if verdict == Verdict.WAIT:
            await self._release(job, timedelta(seconds=self._settings.reconcile_interval_seconds))
            return
        if verdict != Verdict.SUCCEEDED:
            await self._block_rollback(job)
            return
        async with self._sessions.begin() as session:
            jobs = JobRepository(session)
            await jobs.get_owned_job(job, self._worker_id)
            current = await ReleaseRepository(session).get_by_id(release.id, for_update=True)
            current.status = ReleaseStatus.ROLLED_BACK
            if status is not None:
                current.argo_sync_status = status.sync_status
                current.argo_health_status = status.health_status
            request = await DeploymentRequestRepository(session).get_by_id_for_update(
                job.deployment_request_id
            )
            releases = await ReleaseRepository(session).search_by_deployment_request_id(request.id)
            if request.status == DeploymentStatus.FAILED and not any(
                release.status in {ReleaseStatus.PENDING, ReleaseStatus.FAILED}
                for release in releases
            ):
                await _statuses(session).transition_status(request.id, DeploymentStatus.ROLLED_BACK)
            await jobs.mark_succeeded(job, self._worker_id)

    async def _block_rollback(self, job: Job) -> None:
        async with self._sessions.begin() as session:
            jobs = JobRepository(session)
            await jobs.get_owned_job(job, self._worker_id)
            request = await DeploymentRequestRepository(session).get_by_id_for_update(
                job.deployment_request_id
            )
            if request.status == DeploymentStatus.FAILED:
                await _statuses(session).transition_status(
                    request.id, DeploymentStatus.MANUAL_INTERVENTION
                )
            await jobs.mark_manual_intervention(job, self._worker_id, "ROLLBACK_CONDITIONS_NOT_MET")

    async def retry_or_fail(self, job: Job, error_code: str) -> None:
        if job.attempts >= job.max_attempts:
            await self._give_up(job, error_code)
            return
        async with self._sessions.begin() as session:
            await JobRepository(session).retry_later(
                job,
                self._worker_id,
                error_code,
                timedelta(seconds=30 * 2 ** min(job.attempts - 1, 5)),
            )

    async def _give_up(self, job: Job, error_code: str) -> None:
        try:
            async with self._sessions.begin() as session:
                jobs = JobRepository(session)
                await jobs.get_owned_job(job, self._worker_id)
                request = await DeploymentRequestRepository(session).get_by_id_for_update(
                    job.deployment_request_id
                )
                releases = await ReleaseRepository(session).search_by_deployment_request_id(
                    request.id
                )
                if request.status == DeploymentStatus.DEPLOYING:
                    await _statuses(session).transition_status(
                        request.id, DeploymentStatus.FAILED, failure_code=FailureCode.DEPLOY_FAILED
                    )
                if request.status == DeploymentStatus.FAILED and any(
                    release.gitops_commit_sha for release in releases
                ):
                    await _statuses(session).transition_status(
                        request.id, DeploymentStatus.MANUAL_INTERVENTION
                    )
                    await jobs.mark_manual_intervention(job, self._worker_id, error_code)
                else:
                    await jobs.mark_failed(job, self._worker_id, error_code)
        except JobLeaseLostError:
            logger.warning("job lease lost", extra={"action": "fail_deploy"})

    async def _release(self, job: Job, delay: timedelta = timedelta(0)) -> None:
        try:
            async with self._sessions.begin() as session:
                await JobRepository(session).release(job, self._worker_id, delay)
        except JobLeaseLostError:
            logger.warning("job lease lost", extra={"action": "release_deploy"})

    async def _check_owned(self, job: Job) -> None:
        async with self._sessions.begin() as session:
            await JobRepository(session).get_owned_job(job, self._worker_id)

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
            logger.error("deploy lease renewal failed", extra={"action": "renew_deploy_lease"})

    def _path(self, release: Release) -> str:
        path = self._settings.gitops_path_template.format(
            service_id=release.service_id,
            target_id=release.target_id,
            environment=release.environment.value,
        )
        return validate_relative_path(path)

    def _application(self, release: Release) -> str:
        return self._settings.argo_application_template.format(
            service_id=release.service_id,
            target_id=release.target_id,
            environment=release.environment.value,
        )


def _statuses(session: AsyncSession) -> DeploymentStatusService:
    return DeploymentStatusService(
        DeploymentRequestRepository(session),
        DeploymentStatusHistoryRepository(session),
        session=session,
    )
