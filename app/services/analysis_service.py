"""Owned analysis requests and explicit service setting confirmation."""

import re
import uuid
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.analysis_source_client import AnalysisSourceClient
from app.core.analysis_config import AnalysisSettings
from app.core.exceptions import (
    AnalysisCandidateInvalidError,
    AnalysisInProgressError,
    AnalysisNotFoundError,
    AnalysisNotReadyError,
    AnalysisStaleError,
    ExternalError,
    ModelNotConfiguredError,
    RepositoryNotAccessibleError,
    ServiceNotFoundError,
)
from app.enums import AnalysisJobStatus, Builder
from app.models.service import Service
from app.models.service_analysis import ServiceAnalysis
from app.repositories.github_installation_repository import GithubInstallationRepository
from app.repositories.service_analysis_repository import ServiceAnalysisRepository
from app.repositories.service_repository import ServiceRepository
from app.schemas.analysis import ConfirmAnalysisRequest


def source_context(service: Service) -> tuple[str, str, str]:
    return (service.source_repository_url, service.source_branch, service.root_directory or ".")


def analysis_context(analysis: ServiceAnalysis) -> tuple[str, str, str]:
    return (analysis.source_repository_url, analysis.source_branch, analysis.root_directory)


class AnalysisService:
    def __init__(
        self,
        session: AsyncSession,
        service_repository: ServiceRepository,
        analysis_repository: ServiceAnalysisRepository,
        installation_repository: GithubInstallationRepository,
        source_client: AnalysisSourceClient,
        settings: AnalysisSettings,
    ) -> None:
        self._session = session
        self._service_repository = service_repository
        self._analysis_repository = analysis_repository
        self._installation_repository = installation_repository
        self._source_client = source_client
        self._settings = settings

    async def create_analysis(
        self, owner_id: int, service_id: int, mode: Literal["static", "opencode"]
    ) -> ServiceAnalysis:
        service = await self._get_owned(owner_id, service_id)
        if mode == "opencode" and self._settings.model_config_values() is None:
            raise ModelNotConfiguredError("analysis model is not configured")
        fixed_context = source_context(service)
        installation_id = await self._get_installation_id(owner_id, service)
        await self._session.commit()
        sha = await self._source_client.get_head_sha(
            fixed_context[0], fixed_context[1], installation_id
        )
        if re.fullmatch(r"[a-f0-9]{40}", sha) is None:
            raise ExternalError("source client returned an invalid commit identity")
        service = await self._get_owned(owner_id, service_id, for_update=True)
        if source_context(service) != fixed_context:
            raise AnalysisStaleError("service source changed during analysis admission")
        if await self._analysis_repository.find_active(service_id) is not None:
            raise AnalysisInProgressError(
                "an analysis is already in progress", service_id=service_id
            )
        analysis = await self._analysis_repository.save(
            ServiceAnalysis(
                id=str(uuid.uuid4()),
                service_id=service_id,
                requested_by=owner_id,
                source_repository_url=fixed_context[0],
                source_branch=fixed_context[1],
                root_directory=fixed_context[2],
                source_sha=sha,
                github_installation_id=installation_id,
                mode=mode,
                model_selection=self._settings.model_selection() if mode == "opencode" else None,
                status=AnalysisJobStatus.QUEUED,
                stage="queued",
                review_required=True,
            )
        )
        await self._session.commit()
        return analysis

    async def get_latest_analysis(self, owner_id: int, service_id: int) -> ServiceAnalysis:
        await self._get_owned(owner_id, service_id)
        analysis = await self._analysis_repository.find_latest(service_id)
        if analysis is None:
            raise AnalysisNotFoundError("service has no analysis", service_id=service_id)
        return analysis

    async def cancel_analysis(
        self, owner_id: int, service_id: int, analysis_id: str
    ) -> ServiceAnalysis:
        await self._get_owned(owner_id, service_id, for_update=True)
        analysis = await self._get_analysis(service_id, analysis_id, for_update=True)
        if analysis.status == AnalysisJobStatus.CANCELLED:
            return analysis
        if analysis.status not in (AnalysisJobStatus.QUEUED, AnalysisJobStatus.RUNNING):
            raise AnalysisNotReadyError("completed analysis cannot be cancelled")
        analysis.status = AnalysisJobStatus.CANCELLED
        analysis.stage = "cancelled"
        analysis.lease_token = None
        analysis.locked_until = None
        await self._session.commit()
        return analysis

    async def confirm_analysis(
        self, owner_id: int, service_id: int, request: ConfirmAnalysisRequest
    ) -> ServiceAnalysis:
        service = await self._get_owned(owner_id, service_id)
        analysis = await self._get_analysis(service_id, request.analysis_id)
        installation_id = await self._get_installation_id(owner_id, service)
        branch = analysis.source_branch
        repository_url = analysis.source_repository_url
        source_sha = analysis.source_sha
        await self._session.commit()
        head = await self._source_client.get_head_sha(repository_url, branch, installation_id)
        service = await self._get_owned(owner_id, service_id, for_update=True)
        analysis = await self._get_analysis(service_id, request.analysis_id, for_update=True)
        latest = await self._analysis_repository.find_latest(service_id)
        if latest is None or latest.id != analysis.id or head != source_sha:
            raise AnalysisStaleError("analysis no longer matches the current service head")
        if (
            analysis.status != AnalysisJobStatus.SUCCEEDED
            or analysis.analysis_result is None
            or analysis.analysis_status == "unsupported"
        ):
            raise AnalysisNotReadyError("analysis has not completed successfully")
        if analysis.confirmed_at is not None:
            selected_root = self._get_candidate_root(analysis, request.service_candidate_id)
            if (
                analysis.selected_service_candidate_id != request.service_candidate_id
                or source_context(service) != (repository_url, branch, selected_root)
                or service.builder != request.builder
                or service.dockerfile_path
                != (
                    request.dockerfile_path or "Dockerfile"
                    if request.builder == Builder.DOCKERFILE
                    else None
                )
                or any(
                    getattr(service, field) != getattr(request, field)
                    for field in ("port", "build_command", "start_command")
                    if field in request.model_fields_set
                )
            ):
                raise AnalysisStaleError("create a new analysis to change the selected candidate")
            return analysis
        if source_context(service) != analysis_context(analysis):
            raise AnalysisStaleError("analysis no longer matches the service source settings")
        if await self._get_installation_id(owner_id, service) != installation_id:
            raise AnalysisStaleError("service installation changed during confirmation")
        selected_root = self._get_candidate_root(analysis, request.service_candidate_id)
        service.root_directory = None if selected_root == "." else selected_root
        service.builder = request.builder
        if request.builder == Builder.RAILPACK:
            service.dockerfile_path = None
        else:
            service.dockerfile_path = request.dockerfile_path or "Dockerfile"
        for field in ("port", "build_command", "start_command"):
            if field in request.model_fields_set:
                setattr(service, field, getattr(request, field))
        # The verified analyzer result remains immutable; this records only explicit user selection.
        service.analysis_plan = {
            "analysisId": analysis.id,
            "sourceSha": analysis.source_sha,
            "sourceSnapshotId": analysis.source_snapshot_id,
            "contextHash": analysis.context_hash,
            "resultDigest": analysis.result_digest,
            "serviceCandidateId": request.service_candidate_id,
            "deploymentAuthorized": False,
        }
        analysis.selected_service_candidate_id = request.service_candidate_id
        analysis.confirmed_at = datetime.now(UTC)
        questions = analysis.analysis_result.get("questions", [])
        analysis.review_required = analysis.analysis_status != "complete" or (
            isinstance(questions, list)
            and any(
                isinstance(question, dict) and question.get("kind") == "code_review"
                for question in questions
            )
        )
        await self._session.commit()
        return analysis

    async def _get_owned(
        self, owner_id: int, service_id: int, *, for_update: bool = False
    ) -> Service:
        method = (
            self._service_repository.find_by_id_and_owner_id_for_update
            if for_update
            else self._service_repository.find_by_id_and_owner_id
        )
        service = await method(service_id, owner_id)
        if service is None:
            raise ServiceNotFoundError("service not found", service_id=service_id)
        return service

    async def _get_analysis(
        self, service_id: int, analysis_id: str, *, for_update: bool = False
    ) -> ServiceAnalysis:
        analysis = await self._analysis_repository.find_by_id(
            analysis_id, service_id, for_update=for_update
        )
        if analysis is None:
            raise AnalysisNotFoundError("analysis not found", service_id=service_id)
        return analysis

    async def _get_installation_id(self, owner_id: int, service: Service) -> int:
        installations = await self._installation_repository.search_by_user_id(owner_id)
        installation = next(
            (row for row in installations if row.id == service.github_installation_id), None
        )
        if installation is None:
            raise RepositoryNotAccessibleError("service installation is not accessible")
        return installation.installation_id

    @staticmethod
    def _get_candidate_root(analysis: ServiceAnalysis, candidate_id: str) -> str:
        assert analysis.analysis_result is not None
        candidates = analysis.analysis_result.get("services")
        if not isinstance(candidates, list):
            raise AnalysisCandidateInvalidError("analysis has no selectable service candidates")
        for candidate in candidates:
            if not isinstance(candidate, dict) or candidate.get("serviceId") != candidate_id:
                continue
            root = candidate.get("root")
            value = root.get("value") if isinstance(root, dict) else None
            if not isinstance(value, str) or not value:
                break
            path = PurePosixPath(value)
            if (
                path.is_absolute()
                or ".." in path.parts
                or "\\" in value
                or any(ord(char) < 32 for char in value)
            ):
                break
            roots = candidate.get("componentRoots")
            if (
                analysis.root_directory != "."
                and value != analysis.root_directory
                and (not isinstance(roots, list) or analysis.root_directory not in roots)
            ):
                break
            return path.as_posix()
        raise AnalysisCandidateInvalidError("candidate does not match the analyzed service root")
