"""Durable coordinator: fixed source analysis, answered planning, build, deploy, diagnosis."""

import asyncio
import copy
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.analysis_source_client import AnalysisSourceClient
from app.clients.analyzer_client import LocalAnalyzerClient
from app.core.analysis_config import AnalysisSettings
from app.core.exceptions import AppError, InvalidInputError, PipelineStaleError
from app.enums import AnalysisJobStatus, DeploymentStatus, JobKind, PipelineStatus, TargetKind
from app.models.deployment_request import DeploymentRequest
from app.models.deployment_status_history import DeploymentStatusHistory
from app.models.job import Job
from app.models.pipeline_run import PipelineRun
from app.models.service_analysis import ServiceAnalysis
from app.repositories.deployment_request_repository import DeploymentRequestRepository
from app.repositories.github_installation_repository import GithubInstallationRepository
from app.repositories.pipeline_repository import PipelineRepository
from app.repositories.service_analysis_repository import ServiceAnalysisRepository
from app.repositories.service_repository import ServiceRepository
from app.repositories.target_repository import TargetRepository
from app.services.pipeline_contract import digest
from app.services.pipeline_planning import (
    make_pipeline_plan,
    planning_request,
    resolve_pipeline_inputs,
    unresolved_planning_questions,
)
from app.services.pipeline_service import FINAL_PIPELINE_STATUSES, validate_current_pipeline_inputs


class PipelineWorkerService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        source_client: AnalysisSourceClient,
        analyzer_client: LocalAnalyzerClient,
        settings: AnalysisSettings,
    ) -> None:
        self._session_factory = session_factory
        self._source_client = source_client
        self._analyzer_client = analyzer_client
        self._settings = settings

    async def tick(self) -> bool:
        async with self._session_factory.begin() as session:
            run = await PipelineRepository(session).claim_next(self._settings.lease_seconds)
        if run is None:
            return False
        assert run.lease_token is not None
        token = run.lease_token
        task = asyncio.create_task(self._advance(run, token))
        heartbeat = asyncio.create_task(self._heartbeat(run.id, token, task))
        try:
            await task
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
        except AppError as error:
            await self._fail(run.id, token, error.code)
        except Exception:
            await self._fail(run.id, token, "PIPELINE_FAILED")
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            async with self._session_factory.begin() as session:
                await PipelineRepository(session).release_lease(run.id, token)
        return True

    async def _heartbeat(self, run_id: str, token: str, task: asyncio.Task[None]) -> None:
        try:
            while not task.done():
                await asyncio.sleep(self._settings.lease_seconds / 3)
                async with self._session_factory.begin() as session:
                    retained = await PipelineRepository(session).renew_lease(
                        run_id, token, self._settings.lease_seconds
                    )
                if not retained:
                    task.cancel()
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            task.cancel()

    async def _advance(self, run: PipelineRun, token: str) -> None:
        if run.status == PipelineStatus.QUEUED:
            await self._start_analysis(run, token)
        elif run.status in {PipelineStatus.ANALYZING, PipelineStatus.PLANNING}:
            await self._plan(run, token)
        elif run.status in {PipelineStatus.BUILDING, PipelineStatus.DEPLOYING}:
            await self._deployment_progress(run, token)

    async def _start_analysis(self, run: PipelineRun, token: str) -> None:
        if run.mode == "opencode" and run.model_selection != self._settings.model_selection():
            raise InvalidInputError("model configuration changed before pipeline analysis")
        head = await self._source_client.get_head_sha(
            run.source_repository_url, run.source_branch, run.github_installation_id
        )
        if head != run.source_sha:
            await self._fail(run.id, token, "PIPELINE_SUPERSEDED")
            return
        async with self._session_factory.begin() as session:
            service = await ServiceRepository(session).find_by_id_and_owner_id_for_update(
                run.service_id, run.requested_by
            )
            repository = PipelineRepository(session)
            row = await repository.find_owned_lease(run.id, token)
            if row is None or row.status != PipelineStatus.QUEUED:
                return
            if service is None or (
                service.source_repository_url,
                service.source_branch,
                service.root_directory or ".",
            ) != (run.source_repository_url, run.source_branch, run.root_directory):
                raise PipelineStaleError("pipeline source no longer matches the service")
            await validate_current_pipeline_inputs(session, service, row)
            active = await repository.find_active(service.id)
            if active is not None and active.id != row.id:
                row.stage = "waiting_for_previous_pipeline"
                return
            analyses = ServiceAnalysisRepository(session)
            if await analyses.find_active(service.id):
                row.stage = "waiting_for_analysis"
                return
            installations = await GithubInstallationRepository(session).search_by_user_id(
                run.requested_by
            )
            if not any(
                item.id == service.github_installation_id
                and item.installation_id == run.github_installation_id
                for item in installations
            ):
                raise PipelineStaleError("pipeline installation access changed")
            analysis = await analyses.save(
                ServiceAnalysis(
                    id=str(uuid.uuid4()),
                    service_id=service.id,
                    requested_by=run.requested_by,
                    source_repository_url=run.source_repository_url,
                    source_branch=run.source_branch,
                    source_sha=run.source_sha,
                    root_directory=run.root_directory,
                    github_installation_id=run.github_installation_id,
                    mode=run.mode,
                    model_selection=run.model_selection,
                    status=AnalysisJobStatus.QUEUED,
                    stage="queued",
                    review_required=True,
                )
            )
            row.analysis_id = analysis.id
            row.status = PipelineStatus.ANALYZING
            row.stage = "analyzing"

    async def _plan(self, run: PipelineRun, token: str) -> None:
        async with self._session_factory.begin() as session:
            row = await PipelineRepository(session).find_owned_lease(run.id, token)
            if (
                row is None
                or not row.analysis_id
                or row.status not in {PipelineStatus.ANALYZING, PipelineStatus.PLANNING}
            ):
                return
            analysis = await ServiceAnalysisRepository(session).find_by_id(
                row.analysis_id, row.service_id
            )
            if analysis is None:
                raise InvalidInputError("pipeline analysis disappeared")
            if analysis.status in {AnalysisJobStatus.QUEUED, AnalysisJobStatus.RUNNING}:
                row.stage = analysis.stage
                return
            if analysis.status != AnalysisJobStatus.SUCCEEDED:
                row.status = PipelineStatus.FAILED
                row.stage = "analysis_failed"
                row.error_code = analysis.error_code or "ANALYSIS_FAILED"
                return
            if row.stage == "planning_model_started":
                row.status = PipelineStatus.FAILED
                row.stage = "failed"
                row.error_code = "PLANNING_RECOVERY_UNCERTAIN"
                return
            resolved, questions = resolve_pipeline_inputs(row, analysis)
            if questions:
                row.questions = questions
                row.status = PipelineStatus.AWAITING_INPUT
                row.stage = "awaiting_input"
                return
            targets = await TargetRepository(session).search_by_ids(row.target_ids)
            if len(targets) != len(set(row.target_ids)) or any(
                target.kind != TargetKind.AWS for target in targets
            ):
                raise InvalidInputError("pipeline targets are missing or unsupported")
            bindings = [
                {
                    "id": target.id,
                    "kind": target.kind.value,
                    "name": target.name,
                    "region": target.region,
                    "clusterRef": target.cluster_ref,
                    "domainSuffix": target.domain_suffix,
                }
                for target in targets
            ]
            request = planning_request(row, resolved, bindings)
            row.status = PipelineStatus.PLANNING
            row.stage = "planning_model_started" if row.mode == "opencode" else "planning"
            immutable_run = copy.copy(row)
        assert analysis.analysis_result is not None and analysis.source_readiness is not None
        if run.mode == "opencode" and run.model_selection != self._settings.model_selection():
            raise InvalidInputError("planning configuration changed")
        dossier, report = await self._analyzer_client.plan(
            analysis.analysis_result,
            analysis.source_readiness,
            request,
            run.mode,
        )
        planning_questions = unresolved_planning_questions(dossier)
        if planning_questions:
            async with self._session_factory.begin() as session:
                row = await PipelineRepository(session).find_owned_lease(run.id, token)
                if row is not None and row.status == PipelineStatus.PLANNING:
                    row.deployment_dossier = dossier
                    row.planning_report = report
                    row.questions = planning_questions
                    row.status = PipelineStatus.AWAITING_INPUT
                    row.stage = "awaiting_input"
            return
        plan = make_pipeline_plan(immutable_run, analysis, resolved, bindings, dossier)
        head = await self._source_client.get_head_sha(
            run.source_repository_url, run.source_branch, run.github_installation_id
        )
        if head != run.source_sha:
            raise PipelineStaleError("source advanced before pipeline build")
        async with self._session_factory.begin() as session:
            service = await ServiceRepository(session).find_by_id_and_owner_id_for_update(
                run.service_id, run.requested_by
            )
            row = await PipelineRepository(session).find_owned_lease(run.id, token)
            if row is None or row.status != PipelineStatus.PLANNING:
                return
            if service is None or (
                service.source_repository_url,
                service.source_branch,
                service.root_directory or ".",
            ) != (run.source_repository_url, run.source_branch, run.root_directory):
                raise PipelineStaleError("service source changed before build admission")
            await validate_current_pipeline_inputs(session, service, row)
            row.questions = []
            row.deployment_dossier = dossier
            row.planning_report = report
            row.execution_plan = plan
            row.plan_digest = digest(plan)
            config = dict(plan["buildConfig"])
            config["analysis_plan_digest"] = row.plan_digest
            if not row.auto_deploy:
                row.status = PipelineStatus.SUCCEEDED
                row.stage = "plan_ready"
                return
            requests = DeploymentRequestRepository(session)
            deployment = await requests.add_if_absent(
                DeploymentRequest(
                    service_id=row.service_id,
                    environment="prod",
                    source_sha=row.source_sha,
                    source_commit_message=None,
                    trigger_type=row.trigger_type,
                    idempotency_key="pipeline:" + row.id,
                    requested_by=row.requested_by,
                    variables_snapshot={"bindings": resolved["variables"]},
                )
            )
            if deployment is None:
                deployment = await requests.find_by_idempotency_key("pipeline:" + row.id)
            if deployment is None:
                raise InvalidInputError("another deployment is active for this service")
            existing = await session.scalar(select_job(deployment.id))
            if existing is None:
                session.add(
                    DeploymentStatusHistory(
                        deployment_request_id=deployment.id,
                        from_status=None,
                        to_status=DeploymentStatus.QUEUED,
                    )
                )
                session.add(
                    Job(
                        deployment_request_id=deployment.id,
                        kind=JobKind.BUILD,
                        payload={
                            "source_repository_url": row.source_repository_url,
                            "source_branch": row.source_branch,
                            "source_sha": row.source_sha,
                            "root_directory": config["root_directory"],
                            "pipeline_run_id": row.id,
                            "analysis_plan_digest": row.plan_digest,
                            "github_installation_id": row.github_installation_id,
                            "target_ids": row.target_ids,
                            "build_config": config,
                        },
                    )
                )
            row.deployment_request_id = deployment.id
            row.status = PipelineStatus.BUILDING
            row.stage = "build_queued"
            service.builder = config["builder"]
            service.dockerfile_path = config["dockerfile_path"]
            service.root_directory = (
                None if config["root_directory"] == "." else config["root_directory"]
            )
            service.port = config["port"]
            service.build_command = config["build_command"]
            service.start_command = config["start_command"]
            service.railpack_version = config["railpack_version"]
            service.analysis_plan = {
                **(service.analysis_plan or {}),
                "pipelineManaged": True,
                "workflowMode": row.mode,
                "pipelineRunId": row.id,
                "planDigest": row.plan_digest,
                "variableBindings": resolved["variables"],
            }

    async def _deployment_progress(self, run: PipelineRun, token: str) -> None:
        async with self._session_factory.begin() as session:
            row = await PipelineRepository(session).find_owned_lease(run.id, token)
            if row is None or row.deployment_request_id is None:
                return
            deployment = await session.get(DeploymentRequest, row.deployment_request_id)
            if deployment is None:
                raise InvalidInputError("pipeline deployment disappeared")
            if deployment.status in {DeploymentStatus.QUEUED, DeploymentStatus.BUILDING}:
                row.status = PipelineStatus.BUILDING
                row.stage = deployment.status.value.lower()
            elif deployment.status == DeploymentStatus.DEPLOYING:
                row.status = PipelineStatus.DEPLOYING
                row.stage = "deploying"
            elif deployment.status == DeploymentStatus.SUCCEEDED:
                row.status = PipelineStatus.SUCCEEDED
                row.stage = "succeeded"
            else:
                row.status = PipelineStatus.FAILED
                row.stage = "deployment_failed"
                row.error_code = (
                    deployment.failure_code.value
                    if deployment.failure_code
                    else "DEPLOYMENT_FAILED"
                )
                from app.services.diagnosis_service import enqueue_deployment_diagnosis

                await enqueue_deployment_diagnosis(session, deployment)

    async def _fail(self, run_id: str, token: str, code: str) -> None:
        async with self._session_factory.begin() as session:
            row = await PipelineRepository(session).find_owned_lease(run_id, token)
            if row and row.status not in FINAL_PIPELINE_STATUSES:
                row.status = PipelineStatus.FAILED
                row.stage = "failed"
                row.error_code = code


def select_job(deployment_id: int) -> Any:
    from sqlalchemy import select

    return (
        select(Job.id)
        .where(Job.deployment_request_id == deployment_id, Job.kind == JobKind.BUILD)
        .limit(1)
    )
