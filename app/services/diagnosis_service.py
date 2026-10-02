"""Authorize owned deployment diagnosis and enqueue without calling a model."""

from typing import Literal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.diagnosis_config import DiagnosisSettings, get_diagnosis_settings
from app.core.exceptions import (
    ConflictError,
    DeploymentRequestNotFoundError,
    ModelNotConfiguredError,
    NotFoundError,
    ServiceNotFoundError,
)
from app.enums import DeploymentStatus, DiagnosisJobStatus, FailureCode
from app.models.deployment_diagnosis import DeploymentDiagnosis
from app.models.deployment_request import DeploymentRequest
from app.models.project import Project
from app.models.service import Service
from app.repositories.deployment_request_repository import DeploymentRequestRepository
from app.repositories.diagnosis_repository import DiagnosisRepository
from app.repositories.service_repository import ServiceRepository


class DiagnosisNotFoundError(NotFoundError):
    code = "DIAGNOSIS_NOT_FOUND"


class DeploymentNotFailedError(ConflictError):
    code = "DEPLOYMENT_NOT_FAILED"


def deployment_context(deployment: DeploymentRequest) -> dict[str, object]:
    return {
        "reported_stage": "build"
        if deployment.failure_code in (FailureCode.BUILD_FAILED, FailureCode.BUILD_CONFIG_REQUIRED)
        else "deploy",
        "deployment_status": "failed",
        # CodeBuild/Argo do not expose a trustworthy process exit code in the current contract.
        "exit_code": None,
    }


async def enqueue_deployment_diagnosis(
    session: AsyncSession,
    deployment: DeploymentRequest,
    *,
    settings: DiagnosisSettings | None = None,
    requested_by: int | None = None,
    trigger: Literal["deployment_failed", "user_requested"] = "deployment_failed",
) -> DeploymentDiagnosis | None:
    """Automatic hook; uses the caller's transaction and never changes deployment state.

    Each DeploymentRequest is one immutable deployment attempt. A redeployment
    receives a new request ID, so retrying this hook cannot dispatch another model.
    """
    if deployment.status != DeploymentStatus.FAILED:
        return None
    if requested_by is None:
        requested_by = (
            await session.scalars(
                select(Project.owner_id)
                .join(Service, Service.project_id == Project.id)
                .where(
                    Service.id == deployment.service_id,
                    Service.is_deleted.is_(False),
                    Project.is_deleted.is_(False),
                )
            )
        ).one_or_none()
    if requested_by is None:
        return None
    configuration = settings or get_diagnosis_settings()
    return await DiagnosisRepository(session).add_if_absent(
        DeploymentDiagnosis(
            id=str(uuid4()),
            service_id=deployment.service_id,
            deployment_id=deployment.id,
            requested_by=requested_by,
            attempt_id=f"deployment-{deployment.id}",
            trigger=trigger,
            status=DiagnosisJobStatus.QUEUED,
            stage="queued",
            model_selection=configuration.model_selection(),
            deployment_context=deployment_context(deployment),
        )
    )


class DiagnosisService:
    def __init__(self, session: AsyncSession, settings: DiagnosisSettings) -> None:
        self._session = session
        self._settings = settings

    async def _get_owned(
        self, owner_id: int, service_id: int, deployment_id: int
    ) -> DeploymentRequest:
        service = await ServiceRepository(self._session).find_by_id_and_owner_id(
            service_id, owner_id
        )
        if service is None:
            raise ServiceNotFoundError("service not found", service_id=service_id)
        deployment = await DeploymentRequestRepository(self._session).find_by_id_and_service_id(
            deployment_id, service_id
        )
        if deployment is None:
            raise DeploymentRequestNotFoundError("deployment request not found")
        return deployment

    async def create_diagnosis(
        self, owner_id: int, service_id: int, deployment_id: int
    ) -> DeploymentDiagnosis:
        deployment = await self._get_owned(owner_id, service_id, deployment_id)
        if deployment.status != DeploymentStatus.FAILED:
            raise DeploymentNotFailedError("only failed deployment attempts can be diagnosed")
        existing = await DiagnosisRepository(self._session).find_latest(deployment_id)
        if existing is not None:
            return existing
        if self._settings.model_selection() is None:
            raise ModelNotConfiguredError("diagnosis model is not configured")
        if (
            self._settings.input_usd_per_million is None
            or self._settings.output_usd_per_million is None
        ):
            raise ModelNotConfiguredError("diagnosis token pricing is not configured")
        row = await enqueue_deployment_diagnosis(
            self._session,
            deployment,
            settings=self._settings,
            requested_by=owner_id,
            trigger="user_requested",
        )
        assert row is not None
        await self._session.commit()
        return row

    async def get_latest_diagnosis(
        self, owner_id: int, service_id: int, deployment_id: int
    ) -> DeploymentDiagnosis:
        await self._get_owned(owner_id, service_id, deployment_id)
        row = await DiagnosisRepository(self._session).find_latest(deployment_id)
        if row is None:
            raise DiagnosisNotFoundError("deployment has no diagnosis")
        return row
