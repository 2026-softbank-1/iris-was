"""레포 구성 분석(Analysis Gate) 접수·조회·적용. 분석 실행은 Build Worker 가 한다.

단순한 레포(Dockerfile 1개, Railpack 이 바로 빌드하는 앱 1개)는 분석을 생략(skip)하고 기존 서비스
생성 경로를 그대로 쓴다. 복합 레포(analyze)는 분석기가 찾은 배포 단위마다 서비스를 만든다(apply).
"""

import logging
import posixpath
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    FieldIssue,
    InvalidInputError,
    ProjectNotFoundError,
    RepositoryAnalysisNotFoundError,
    RepositoryAnalysisNotReadyError,
)
from app.enums import (
    AnalysisGateDecision,
    AnalysisGateMode,
    Builder,
    DeploymentTrigger,
    RepositoryAnalysisStatus,
    ServiceKind,
)
from app.models.repository_analysis import RepositoryAnalysis
from app.models.service import Service
from app.repositories.github_installation_repository import GithubInstallationRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.repository_analysis_repository import RepositoryAnalysisRepository
from app.schemas.analysis_gate import AnalysisGateResult, AnalysisGateUnit
from app.services.builder_detection import is_valid_docker_target
from app.services.manual_deployment_service import ManualDeploymentService
from app.services.service_registry_service import (
    AnalyzedServicePlan,
    ServiceDetail,
    ServiceRegistryService,
    normalize_root_directory,
    slugify_service_name,
)
from app.services.source_repository_service import SourceRepositoryService
from app.services.stack_apply_service import DependencySelection, StackApplyService
from app.services.stack_service import StackService
from app.services.variable_validation import VariableValidation, VariableValidationService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UnitSelection:
    """apply 에서 고른 배포 단위와 사용자가 고친 값. None 이면 분석 결과 값을 쓴다."""

    unit_id: str
    name: str | None = None
    root_directory: str | None = None
    builder: Builder | None = None
    dockerfile_path: str | None = None
    docker_target: str | None = None
    port: int | None = None
    start_command: str | None = None
    build_command: str | None = None


@dataclass(frozen=True)
class AppliedAnalysis:
    analysis_id: int
    services: list[ServiceDetail]
    databases: list[ServiceDetail] = field(default_factory=list)
    stack_id: int | None = None
    stack_deployment_id: int | None = None
    # 환경변수 error 로 배포를 접수하지 않았으면 그 검증 결과(서비스마다). 서비스는 만들어져 있다.
    variable_validations: list[VariableValidation] = field(default_factory=list)
    # 증분 apply 에서 이미 있는 DB 의 초기화 스크립트가 달라진 것(DEPENDENCY_CHANGED). 다시 실행하지
    # 않는다.
    changes: list[dict[str, Any]] = field(default_factory=list)


class RepositoryAnalysisService:
    def __init__(
        self,
        session: AsyncSession,
        project_repository: ProjectRepository,
        repository_analysis_repository: RepositoryAnalysisRepository,
        installation_repository: GithubInstallationRepository,
        source_repository_service: SourceRepositoryService,
        service_registry_service: ServiceRegistryService,
        manual_deployment_service: ManualDeploymentService,
        *,
        stack_apply_service: StackApplyService | None = None,
        stack_service: StackService | None = None,
        variable_validation_service: VariableValidationService | None = None,
    ) -> None:
        # 셋이 다 있으면 분석 결과를 스택으로 적용한다(DB·참조 변수·별칭·의존 순서 배포).
        self._stack_apply_service = stack_apply_service
        self._stack_service = stack_service
        self._variable_validation_service = variable_validation_service
        self._session = session
        self._project_repository = project_repository
        self._repository_analysis_repository = repository_analysis_repository
        self._installation_repository = installation_repository
        self._source_repository_service = source_repository_service
        self._service_registry_service = service_registry_service
        self._manual_deployment_service = manual_deployment_service

    async def create_analysis(
        self,
        owner_id: int,
        project_id: int,
        repository_url: str,
        branch: str | None,
        root_directory: str | None,
        mode: AnalysisGateMode,
        github_installation_id: int | None = None,
    ) -> RepositoryAnalysis:
        """분석을 접수한다. 브랜치 최신 커밋을 지금 고정해, 분석과 이후 배포가 같은 소스를 본다."""
        project = await self._project_repository.find_by_id_and_owner_id(project_id, owner_id)
        if project is None:
            raise ProjectNotFoundError("project not found", project_id=project_id)
        repository = await self._source_repository_service.resolve_repository(
            owner_id, repository_url
        )
        if github_installation_id is not None and github_installation_id != (
            repository.installation_id
        ):
            raise InvalidInputError(
                "github installation does not grant this repository",
                issues=[FieldIssue("githubInstallationId", "installation_mismatch")],
                field="githubInstallationId",
            )
        branch = branch or repository.default_branch
        head = await self._source_repository_service.find_branch_head(
            owner_id, repository.full_name, branch
        )
        if head is None:
            raise InvalidInputError("branch not found in repository", field="sourceBranch")
        installation = await self._installation_repository.find_by_installation_id(
            repository.installation_id
        )
        analysis = await self._repository_analysis_repository.save(
            RepositoryAnalysis(
                project_id=project.id,
                user_id=owner_id,
                source_repository_url=repository.url,
                github_installation_id=installation.id if installation is not None else None,
                source_branch=branch,
                source_sha=head.sha,
                root_directory=normalize_root_directory(root_directory),
                mode=mode,
                status=RepositoryAnalysisStatus.QUEUED,
            )
        )
        await self._session.commit()
        logger.info(
            "repository analysis requested",
            extra={
                "action": "create_analysis",
                "project_id": project.id,
                "repository_analysis_id": analysis.id,
                "mode": mode,
            },
        )
        return analysis

    async def get_analysis(
        self, owner_id: int, project_id: int, analysis_id: int
    ) -> RepositoryAnalysis:
        return await self._get_owned(owner_id, project_id, analysis_id)

    async def apply_analysis(
        self,
        owner_id: int,
        project_id: int,
        analysis_id: int,
        selections: list[UnitSelection],
        *,
        should_deploy: bool,
        target_ids: list[int] | None = None,
        is_auto_deploy: bool = True,
        dependencies: list[DependencySelection] | None = None,
        skip_variable_validation: bool = False,
    ) -> AppliedAnalysis:
        """고른 배포 단위마다 서비스를 만든다. 이미 적용했으면 그때 만든 서비스를 돌려준다.

        스택으로 적용하면 같은 트랜잭션에서 DB 서비스 → 앱 서비스 → 참조 변수 → 호스트 별칭을
        만들고(같은 레포의 스택이 있으면 unit id 로 맞춰 증분 적용), 배포는 DB 가 먼저 성공한 뒤
        앱이
        시작하는 스택 배포 하나로 접수한다. 환경변수 error 가 있으면 서비스는 남기고 배포를 접수하지
        않으며 검증 결과를 돌려준다. 같은 키라 다시 보내면 빠진 배포만 새로 만든다.
        """
        analysis = await self._get_owned(owner_id, project_id, analysis_id, for_update=True)
        if not self._is_stack_mode:
            return await self._apply_without_stack(
                owner_id, analysis, selections, should_deploy, target_ids, is_auto_deploy
            )
        assert self._stack_apply_service is not None and self._stack_service is not None
        changes: list[dict[str, Any]] = []
        if analysis.status != RepositoryAnalysisStatus.APPLIED:
            plans = self._plans(analysis, selections)
            applied = await self._stack_apply_service.apply(
                owner_id,
                analysis,
                plans,
                dependencies,
                target_ids,
                is_auto_deploy=is_auto_deploy,
            )
            analysis.stack_id = analysis.stack_id or applied.stack.id
            changes = applied.changes
            analysis.mark_as_applied(
                [s.id for s in applied.apps] + [s.id for s in applied.databases]
            )
            await self._session.commit()
            logger.info(
                "repository analysis applied",
                extra={
                    "action": "apply_analysis",
                    "repository_analysis_id": analysis.id,
                    "stack_id": applied.stack.id,
                    "service_ids": analysis.applied_service_ids,
                },
            )
        service_ids = list(analysis.applied_service_ids or [])
        details = await self._service_registry_service.search_services_by_ids(owner_id, service_ids)
        stack_id = details[0].service.stack_id if details else analysis.stack_id
        stack_deployment_id: int | None = None
        validations: list[VariableValidation] = []
        if should_deploy and analysis.source_sha is not None and stack_id is not None:
            stack_deployment_id, validations = await self._deploy_stack(
                owner_id, analysis, stack_id, [d.service for d in details], skip_variable_validation
            )
            details = await self._service_registry_service.search_services_by_ids(
                owner_id, service_ids
            )
        return AppliedAnalysis(
            analysis_id=analysis_id,
            services=[d for d in details if d.service.kind == ServiceKind.APP],
            databases=[d for d in details if d.service.kind == ServiceKind.DATABASE],
            stack_id=stack_id,
            stack_deployment_id=stack_deployment_id,
            variable_validations=validations,
            changes=changes,
        )

    @property
    def _is_stack_mode(self) -> bool:
        return self._stack_apply_service is not None and self._stack_service is not None

    async def _deploy_stack(
        self,
        owner_id: int,
        analysis: RepositoryAnalysis,
        stack_id: int,
        services: list[Service],
        skip_variable_validation: bool,
    ) -> tuple[int | None, list[VariableValidation]]:
        assert self._stack_service is not None and analysis.source_sha is not None
        key = f"analysis:{analysis.id}"
        if not skip_variable_validation and self._variable_validation_service is not None:
            validations = [await self._variable_validation_service.validate(s) for s in services]
            failed = [v for v in validations if not v.ok]
            if failed:
                logger.info(
                    "stack deployment blocked by variables",
                    extra={
                        "action": "apply_analysis",
                        "repository_analysis_id": analysis.id,
                        "service_ids": [v.service_id for v in failed],
                    },
                )
                return None, failed
        stack = await self._stack_service.get_stack_model(stack_id)
        # 이미 떠 있는 DB 는 다시 띄우지 않는다(증분 apply).
        services = [s for s in services if not await self._stack_service.is_live_database(s)]
        source_sha = analysis.source_sha

        async def source() -> tuple[str, str | None]:
            return source_sha, None

        created = await self._stack_service.create_stack_deployment(
            stack,
            services,
            trigger_type=DeploymentTrigger.MANUAL,
            idempotency_key=key,
            requested_by=owner_id,
            source=source,
            is_strict=False,
        )
        await self._session.commit()
        return created.stack_deployment.id, []

    async def _apply_without_stack(
        self,
        owner_id: int,
        analysis: RepositoryAnalysis,
        selections: list[UnitSelection],
        should_deploy: bool,
        target_ids: list[int] | None,
        is_auto_deploy: bool,
    ) -> AppliedAnalysis:
        """스택 구성 요소가 없을 때(이전 동작): 서비스만 만들고 서비스마다 배포 요청을 만든다."""
        if analysis.status != RepositoryAnalysisStatus.APPLIED:
            await self._create_services(owner_id, analysis, selections, target_ids, is_auto_deploy)
        service_ids = list(analysis.applied_service_ids or [])
        source_sha = analysis.source_sha
        if should_deploy and source_sha is not None:
            for service_id in service_ids:
                await self._manual_deployment_service.create_deployment_request(
                    owner_id,
                    service_id,
                    trigger_type=DeploymentTrigger.MANUAL,
                    source_sha=source_sha,
                    idempotency_key=f"analysis-{analysis.id}",
                )
        services = await self._service_registry_service.search_services_by_ids(
            owner_id, service_ids
        )
        return AppliedAnalysis(analysis_id=analysis.id, services=services)

    def _plans(
        self, analysis: RepositoryAnalysis, selections: list[UnitSelection]
    ) -> list[AnalyzedServicePlan]:
        if (
            analysis.status != RepositoryAnalysisStatus.SUCCEEDED
            or analysis.decision != AnalysisGateDecision.ANALYZE
            or analysis.result is None
        ):
            raise RepositoryAnalysisNotReadyError(
                "repository analysis has no deployment units to apply",
                analysis_id=analysis.id,
                status=analysis.status,
                decision=analysis.decision,
            )
        result = AnalysisGateResult.model_validate(analysis.result)
        if not selections:
            raise InvalidInputError("at least one unit is required", field="units")
        unit_ids = [selection.unit_id for selection in selections]
        if len(set(unit_ids)) != len(unit_ids):
            raise InvalidInputError("units must not repeat", field="units")
        plans: list[AnalyzedServicePlan] = []
        for selection in selections:
            unit = result.find_unit(selection.unit_id)
            if unit is None:
                raise InvalidInputError(
                    "unit not found in analysis", field="units", unit_id=selection.unit_id
                )
            plans.append(_to_plan(analysis, unit, selection))
        return plans

    async def _create_services(
        self,
        owner_id: int,
        analysis: RepositoryAnalysis,
        selections: list[UnitSelection],
        target_ids: list[int] | None,
        is_auto_deploy: bool,
    ) -> None:
        plans = self._plans(analysis, selections)
        services = await self._service_registry_service.create_analyzed_services(
            owner_id,
            analysis.project_id,
            analysis.source_repository_url,
            analysis.source_branch,
            plans,
            target_ids,
            is_auto_deploy=is_auto_deploy,
        )
        analysis.mark_as_applied([service.id for service in services])
        await self._session.commit()
        logger.info(
            "repository analysis applied",
            extra={
                "action": "apply_analysis",
                "repository_analysis_id": analysis.id,
                "service_ids": analysis.applied_service_ids,
            },
        )

    async def _get_owned(
        self, owner_id: int, project_id: int, analysis_id: int, *, for_update: bool = False
    ) -> RepositoryAnalysis:
        project = await self._project_repository.find_by_id_and_owner_id(project_id, owner_id)
        if project is None:
            raise ProjectNotFoundError("project not found", project_id=project_id)
        analysis = await self._repository_analysis_repository.find_by_id_and_project_id(
            analysis_id, project.id, for_update=for_update
        )
        if analysis is None:
            raise RepositoryAnalysisNotFoundError(
                "repository analysis not found", analysis_id=analysis_id
            )
        return analysis


def _to_plan(
    analysis: RepositoryAnalysis, unit: AnalysisGateUnit, selection: UnitSelection
) -> AnalyzedServicePlan:
    """분석 결과 단위에 사용자가 고친 값을 덮어 서비스 설정을 만든다.

    단위의 rootDirectory 는 레포 루트 기준이고 dockerfilePath 는 그 단위 루트 기준이다. 서비스의
    root_directory·dockerfile_path 도 같은 기준이라 그대로 옮긴다.
    """
    if selection.name is not None:
        name = selection.name
    else:
        name = slugify_service_name(unit.name)
    root_directory = normalize_root_directory(
        selection.root_directory if selection.root_directory is not None else unit.root_directory
    )
    builder = selection.builder or unit.builder
    dockerfile_path = _normalize_relative_path(
        selection.dockerfile_path if selection.dockerfile_path is not None else unit.dockerfile_path
    )
    docker_target = _normalize_docker_target(
        selection.docker_target if selection.docker_target is not None else unit.build_target
    )
    if builder == Builder.RAILPACK:
        dockerfile_path = None
        docker_target = None
    port = selection.port if selection.port is not None else unit.port
    start_command = selection.start_command or unit.start_command
    build_command = selection.build_command or unit.build_command
    applied = {
        "name": name,
        "rootDirectory": root_directory,
        "builder": builder.value if builder is not None else None,
        "dockerfilePath": dockerfile_path,
        "buildTarget": docker_target,
        "port": port,
        "startCommand": start_command,
        "buildCommand": build_command,
    }
    return AnalyzedServicePlan(
        name=name,
        root_directory=root_directory,
        builder=builder,
        dockerfile_path=dockerfile_path,
        docker_target=docker_target,
        port=port,
        start_command=start_command,
        build_command=build_command,
        stack_unit_id=unit.id,
        analysis_plan={
            "gate": analysis.build_gate_plan(unit.id),
            # 분석기가 준 단위 원문(role·env·dependsOn·evidence 포함)과 실제로 쓴 값.
            "unit": {**_find_raw_unit(analysis, unit.id), "applied": applied},
        },
    )


def _find_raw_unit(analysis: RepositoryAnalysis, unit_id: str) -> dict[str, Any]:
    units = (analysis.result or {}).get("units") or []
    return next(
        (dict(unit) for unit in units if isinstance(unit, dict) and unit.get("id") == unit_id),
        {"id": unit_id},
    )


def _normalize_relative_path(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    path = posixpath.normpath(value.strip().replace("\\", "/"))
    if path.startswith("/") or path == ".." or path.startswith("../"):
        raise InvalidInputError("dockerfile path must stay inside the unit", field="dockerfilePath")
    return path


def _normalize_docker_target(value: str | None) -> str | None:
    """분석기 buildTarget·사용자 값. 빈 값이나 받을 수 없는 형식(요청 값은 스키마가 먼저 거른다)이면
    쓰지 않는다(마지막 스테이지로 빌드)."""
    if value is None or not value.strip():
        return None
    value = value.strip()
    return value if is_valid_docker_target(value) else None
