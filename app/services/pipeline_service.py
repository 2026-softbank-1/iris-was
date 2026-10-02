"""Owned admission and user answers; long-running model/build work stays in workers."""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.analysis_source_client import AnalysisSourceClient
from app.core.analysis_config import AnalysisSettings
from app.core.exceptions import (
    IdempotencyConflictError,
    InvalidInputError,
    ModelNotConfiguredError,
    PipelineInProgressError,
    PipelineNotFoundError,
    PipelineNotReadyError,
    PipelineStaleError,
    ServiceNotFoundError,
)
from app.enums import AnalysisJobStatus, DeploymentTrigger, PipelineStatus, TargetKind
from app.models.pipeline_run import PipelineRun
from app.models.project import Project
from app.models.service import Service
from app.repositories.github_installation_repository import GithubInstallationRepository
from app.repositories.pipeline_repository import PipelineRepository
from app.repositories.service_analysis_repository import ServiceAnalysisRepository
from app.repositories.service_repository import ServiceRepository
from app.repositories.target_repository import TargetRepository
from app.schemas.pipeline import PipelineAnswers, StartPipelineRequest
from app.services.pipeline_contract import digest

FINAL_PIPELINE_STATUSES = {
    PipelineStatus.SUCCEEDED,
    PipelineStatus.FAILED,
    PipelineStatus.CANCELLED,
}


class PipelineService:
    def __init__(
        self,
        session: AsyncSession,
        source_client: AnalysisSourceClient,
        settings: AnalysisSettings,
    ) -> None:
        self._session = session
        self._source_client = source_client
        self._settings = settings
        self._repository = PipelineRepository(session)
        self._services = ServiceRepository(session)

    async def _owned(self, owner_id: int, service_id: int, *, lock: bool = False) -> Service:
        method = (
            self._services.find_by_id_and_owner_id_for_update
            if lock
            else self._services.find_by_id_and_owner_id
        )
        service = await method(service_id, owner_id)
        if service is None:
            raise ServiceNotFoundError("service not found", service_id=service_id)
        return service

    async def create_pipeline(
        self,
        owner_id: int,
        service_id: int,
        request: StartPipelineRequest,
        idempotency_key: str | None = None,
    ) -> PipelineRun:
        service = await self._owned(owner_id, service_id)
        if request.mode == "opencode" and self._settings.model_selection() is None:
            raise ModelNotConfiguredError("analysis model is not configured")
        source = (
            service.source_repository_url,
            service.source_branch,
            service.root_directory or ".",
        )
        installations = await GithubInstallationRepository(self._session).search_by_user_id(
            owner_id
        )
        installation = next(
            (item for item in installations if item.id == service.github_installation_id), None
        )
        if installation is None:
            raise InvalidInputError("service installation is not accessible")
        external_installation = installation.installation_id
        await self._session.commit()
        sha = await self._source_client.get_head_sha(source[0], source[1], external_installation)
        service = await self._owned(owner_id, service_id, lock=True)
        if source != (
            service.source_repository_url,
            service.source_branch,
            service.root_directory or ".",
        ):
            raise PipelineStaleError("service source changed during pipeline admission")
        key = f"pipeline:{service_id}:{idempotency_key or uuid.uuid4()}"
        fingerprint = digest(
            {"source": source, "request": request.model_dump(mode="json"), "sourceSha": sha}
        )
        existing = await self._repository.find_by_key(key)
        if existing is not None:
            if existing.request_fingerprint != fingerprint:
                raise IdempotencyConflictError("idempotency key was used with different input")
            return existing
        if await self._repository.find_active(service_id):
            raise PipelineInProgressError("a pipeline is already active", service_id=service_id)
        queued = await self._repository.find_latest(service_id)
        if queued is not None and queued.status == PipelineStatus.QUEUED:
            raise PipelineInProgressError("a pipeline is already queued", service_id=service_id)
        targets = await self._services.search_target_ids_by_service_ids([service_id])
        if not targets[service_id]:
            raise InvalidInputError("select at least one deployment target")
        selected_targets = await TargetRepository(self._session).search_by_ids(targets[service_id])
        if len(selected_targets) != 1 or selected_targets[0].kind != TargetKind.AWS:
            raise InvalidInputError("select one AWS deployment target for this workflow")
        run = await self._repository.save(
            PipelineRun(
                id=str(uuid.uuid4()),
                service_id=service_id,
                requested_by=owner_id,
                source_repository_url=source[0],
                source_branch=source[1],
                root_directory=source[2],
                source_sha=sha,
                github_installation_id=external_installation,
                mode=request.mode,
                trigger_type=DeploymentTrigger.MANUAL,
                idempotency_key=key,
                request_fingerprint=fingerprint,
                auto_deploy=request.auto_deploy,
                enable_auto_deploy=request.enable_auto_deploy,
                config_snapshot=self._config_snapshot(service),
                target_ids=targets[service_id],
                confirmed_inputs={
                    "variables": (service.analysis_plan or {}).get("variableBindings", [])
                },
                questions=[],
                status=PipelineStatus.QUEUED,
                stage="queued",
                model_selection=self._settings.model_selection()
                if request.mode == "opencode"
                else None,
            )
        )
        # Subsequent pushes must use the same analysis-before-plan workflow.
        if request.auto_deploy or request.enable_auto_deploy:
            service.is_auto_deploy = request.enable_auto_deploy
            service.analysis_plan = {
                **(service.analysis_plan or {}),
                "workflowMode": request.mode,
                "pipelineManaged": True,
            }
        await self._session.commit()
        return run

    @staticmethod
    def _config_snapshot(service: Service) -> dict[str, Any]:
        return {
            key: getattr(service, key)
            for key in (
                "builder",
                "dockerfile_path",
                "platform",
                "railpack_version",
                "port",
                "build_command",
                "start_command",
            )
        }

    async def get_latest(self, owner_id: int, service_id: int) -> PipelineRun:
        await self._owned(owner_id, service_id)
        run = await self._repository.find_latest(service_id)
        if run is None:
            raise PipelineNotFoundError("service has no pipeline")
        return run

    async def answer(
        self, owner_id: int, service_id: int, run_id: str, answer: PipelineAnswers
    ) -> PipelineRun:
        service = await self._owned(owner_id, service_id)
        run = await self._repository.find_by_id(run_id)
        if run is None or run.service_id != service_id:
            raise PipelineNotFoundError("pipeline not found")
        pinned = (run.source_repository_url, run.source_branch, run.source_sha, run.root_directory)
        await self._session.commit()
        head = await self._source_client.get_head_sha(
            pinned[0], pinned[1], run.github_installation_id
        )
        service = await self._owned(owner_id, service_id, lock=True)
        run = await self._repository.find_by_id(run_id, for_update=True)
        assert run is not None
        if head != pinned[2] or (
            service.source_repository_url,
            service.source_branch,
            service.root_directory or ".",
        ) != (pinned[0], pinned[1], pinned[3]):
            raise PipelineStaleError("source changed; start a new pipeline")
        if run.status != PipelineStatus.AWAITING_INPUT:
            raise PipelineNotReadyError("pipeline is not waiting for inputs")
        await validate_current_pipeline_inputs(self._session, service, run)
        inputs = dict(run.confirmed_inputs)
        previous_variables = list(inputs.get("variables", []))
        inputs.update(answer.model_dump(mode="json", exclude_unset=True))
        if "variables" in answer.model_fields_set:
            prior = {item["key"]: item for item in previous_variables}
            prior.update(
                {
                    item.key: item.model_dump(mode="json", exclude_none=True)
                    for item in answer.variables
                }
            )
            inputs["variables"] = list(prior.values())
        run.confirmed_inputs = inputs
        run.status = PipelineStatus.PLANNING
        run.stage = "planning"
        run.error_code = None
        await self._session.commit()
        return run

    async def cancel(self, owner_id: int, service_id: int, run_id: str) -> PipelineRun:
        await self._owned(owner_id, service_id, lock=True)
        run = await self._repository.find_by_id(run_id, for_update=True)
        if run is None or run.service_id != service_id:
            raise PipelineNotFoundError("pipeline not found")
        if run.status == PipelineStatus.CANCELLED:
            return run
        if run.status in {PipelineStatus.BUILDING, PipelineStatus.DEPLOYING}:
            raise PipelineNotReadyError(
                "build/deploy already started; cancellation requires executor cleanup"
            )
        if run.status in FINAL_PIPELINE_STATUSES:
            raise PipelineNotReadyError("completed pipeline cannot be cancelled")
        if run.analysis_id:
            analysis = await ServiceAnalysisRepository(self._session).find_by_id(
                run.analysis_id, service_id, for_update=True
            )
            if analysis and analysis.status in {
                AnalysisJobStatus.QUEUED,
                AnalysisJobStatus.RUNNING,
            }:
                analysis.status = AnalysisJobStatus.CANCELLED
                analysis.stage = "cancelled"
                analysis.lease_token = None
                analysis.locked_until = None
        run.status = PipelineStatus.CANCELLED
        run.stage = "cancelled"
        run.lease_token = None
        run.locked_until = None
        await self._session.commit()
        return run


async def enqueue_push_pipeline(
    session: AsyncSession, service: Service, sha: str, delivery_id: str
) -> PipelineRun | None:
    repository = PipelineRepository(session)
    key = f"pipeline-push:{service.id}:{delivery_id}"
    if await repository.find_by_key(key):
        return None
    owner_id = await session.scalar(
        select(Project.owner_id).where(Project.id == service.project_id)
    )
    if owner_id is None:
        return None
    installations = await GithubInstallationRepository(session).search_by_user_id(owner_id)
    installation = next(
        (item for item in installations if item.id == service.github_installation_id), None
    )
    if installation is None:
        return None
    targets = await ServiceRepository(session).search_target_ids_by_service_ids([service.id])
    settings = AnalysisSettings()
    mode = (service.analysis_plan or {}).get("workflowMode", "opencode")
    return await repository.save(
        PipelineRun(
            id=str(uuid.uuid4()),
            service_id=service.id,
            requested_by=owner_id,
            source_repository_url=service.source_repository_url,
            source_branch=service.source_branch,
            source_sha=sha,
            root_directory=service.root_directory or ".",
            github_installation_id=installation.installation_id,
            mode=mode,
            trigger_type=DeploymentTrigger.PUSH,
            idempotency_key=key,
            request_fingerprint=digest({"sha": sha, "serviceId": service.id}),
            auto_deploy=True,
            enable_auto_deploy=True,
            status=PipelineStatus.QUEUED,
            stage="queued",
            config_snapshot=PipelineService._config_snapshot(service),
            target_ids=targets[service.id],
            confirmed_inputs={
                "variables": (service.analysis_plan or {}).get("variableBindings", [])
            },
            questions=[],
            model_selection=settings.model_selection() if mode == "opencode" else None,
        )
    )


async def validate_current_pipeline_inputs(
    session: AsyncSession, service: Service, run: PipelineRun
) -> None:
    if PipelineService._config_snapshot(service) != run.config_snapshot:
        raise PipelineStaleError("service build settings changed during the pipeline")
    target_ids = await ServiceRepository(session).search_target_ids_by_service_ids([service.id])
    if set(target_ids[service.id]) != set(run.target_ids):
        raise PipelineStaleError("service targets changed during the pipeline")
    installations = await GithubInstallationRepository(session).search_by_user_id(run.requested_by)
    if not any(
        item.id == service.github_installation_id
        and item.installation_id == run.github_installation_id
        for item in installations
    ):
        raise PipelineStaleError("pipeline installation access changed")
