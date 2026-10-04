import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    ConflictError,
    FieldIssue,
    InvalidInputError,
    ProjectNotFoundError,
    RepositoryAnalysisNotFoundError,
    RepositoryAnalysisNotReadyError,
    ServiceNameConflictError,
    ServiceNotFoundError,
)
from app.enums import (
    AnalysisGateDecision,
    Builder,
    DeploymentStrategy,
    RepositoryAnalysisStatus,
    ServiceKind,
    TargetKind,
)
from app.models.deployment_request import DeploymentRequest
from app.models.project import Project
from app.models.repository_analysis import RepositoryAnalysis
from app.models.service import Service
from app.models.target import AWS_TARGET_NAME
from app.repositories.deployment_request_repository import DeploymentRequestRepository
from app.repositories.github_installation_repository import GithubInstallationRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.repository_analysis_repository import RepositoryAnalysisRepository
from app.repositories.service_repository import ServiceRepository
from app.repositories.target_repository import TargetRepository
from app.schemas.analysis_gate import AnalysisGateResult
from app.services.deployment_strategy import (
    MIN_PROGRESSIVE_REPLICAS,
    PROGRESSIVE_STRATEGIES,
    PROGRESSIVE_TARGET_KINDS,
)
from app.services.scaling_config import ScalingConfig
from app.services.service_networking import (
    is_networking_available,
    networking_unavailable_reason,
    validate_host_aliases,
)
from app.services.service_teardown_service import ServiceTeardownService
from app.services.source_repository_service import RepositoryCandidate, SourceRepositoryService

logger = logging.getLogger(__name__)

# 서비스 이름은 이후 도메인(서브도메인)에 쓰이므로 DNS 레이블 규칙을 따른다.
SERVICE_NAME_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")

_NULLABLE_FIELDS = frozenset(
    {"root_directory", "dockerfile_path", "port", "build_command", "start_command", "builder"}
)
_NON_NULL_FIELDS = frozenset(
    {"name", "source_branch", "is_auto_deploy", "target_ids", "deployment_strategy"}
)
# null 이면 별칭을 모두 지운다.
_NETWORKING_FIELDS = frozenset({"host_aliases"})
# 관리형 DB 는 소스·빌드가 없어 이 필드를 바꿀 수 없다.
_SOURCE_FIELDS = frozenset(
    {
        "source_branch",
        "root_directory",
        "is_auto_deploy",
        "builder",
        "dockerfile_path",
        "port",
        "build_command",
        "start_command",
        "deployment_strategy",
        "target_ids",
    }
)


@dataclass(frozen=True)
class ServiceDetail:
    service: Service
    target_ids: list[int]
    latest_deployment: DeploymentRequest | None = None
    # 프로젝트 내부 통신(chart 0.8.0)을 쓰는 서비스인지. 내부 포트 계산에 쓴다.
    is_networking: bool = False


@dataclass(frozen=True)
class AnalyzedServicePlan:
    """분석 결과의 배포 단위 하나로 만들 서비스 설정. 경로는 저장소 루트 기준이다."""

    name: str
    root_directory: str | None
    builder: Builder | None
    dockerfile_path: str | None
    port: int | None
    start_command: str | None
    build_command: str | None
    analysis_plan: dict[str, Any]
    # 스택에 넣을 때의 분석기 unit id.
    stack_unit_id: str | None = None


def slugify_service_name(value: str, max_length: int = 63) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:max_length].strip("-")
    return slug or "service"


def normalize_root_directory(value: str | None) -> str | None:
    """저장소 안 상대 경로로 정리한다. 루트(빈 값·`/`·`.`)는 None 이다."""
    if value is None:
        return None
    parts = [p for p in value.strip().replace("\\", "/").split("/") if p not in ("", ".")]
    if ".." in parts:
        raise InvalidInputError(
            "root directory must stay inside the repository", field="rootDirectory"
        )
    return "/".join(parts) or None


class ServiceRegistryService:
    """서비스(사용자 앱) 등록·조회·설정. 저장소 연결은 사용자의 GitHub 접근 권한을 확인한다."""

    def __init__(
        self,
        session: AsyncSession,
        project_repository: ProjectRepository,
        service_repository: ServiceRepository,
        target_repository: TargetRepository,
        installation_repository: GithubInstallationRepository,
        source_repository_service: SourceRepositoryService,
        deployment_request_repository: DeploymentRequestRepository,
        service_teardown_service: ServiceTeardownService,
        *,
        deployment_strategy_enabled: bool = False,
        repository_analysis_repository: RepositoryAnalysisRepository | None = None,
        is_networking_enabled: bool = False,
    ) -> None:
        self._session = session
        self._is_networking_enabled = is_networking_enabled
        self._repository_analysis_repository = repository_analysis_repository
        self._project_repository = project_repository
        self._service_repository = service_repository
        self._service_teardown_service = service_teardown_service
        self._target_repository = target_repository
        self._installation_repository = installation_repository
        self._source_repository_service = source_repository_service
        self._deployment_request_repository = deployment_request_repository
        self._deployment_strategy_enabled = deployment_strategy_enabled

    async def create_service(
        self,
        owner_id: int,
        project_id: int,
        repository_url: str,
        name: str | None,
        branch: str | None,
        root_directory: str | None,
        is_auto_deploy: bool,
        target_ids: list[int] | None,
        *,
        analysis_id: int | None = None,
    ) -> ServiceDetail:
        """`analysis_id` 가 있으면 그 레포 구성 분석의 결정을 `analysis_plan.gate` 에 남긴다.

        분석을 생략(skip)한 결과면 분석기가 고른 빌더·Dockerfile 경로를 서비스 기본값으로 쓴다.
        """
        project = await self._get_project(owner_id, project_id)
        repository = await self._source_repository_service.resolve_repository(
            owner_id, repository_url
        )
        if branch is None:
            branch = repository.default_branch
        else:
            await self._ensure_branch_exists(owner_id, repository.full_name, branch)
        service_name = name or slugify_service_name(repository.full_name.split("/", 1)[1])
        await self._ensure_name_available(project.id, service_name)
        resolved_target_ids = await self._resolve_target_ids(target_ids)
        normalized_root = normalize_root_directory(root_directory)
        service = Service(
            project_id=project.id,
            name=service_name,
            source_repository_url=repository.url,
            github_installation_id=await self._get_installation_row_id(repository),
            source_branch=branch,
            root_directory=normalized_root,
            is_auto_deploy=is_auto_deploy,
        )
        if analysis_id is not None:
            analysis = await self._get_gate_analysis(
                project.id, analysis_id, repository, normalized_root
            )
            _apply_gate_defaults(service, analysis)

        service = await self._service_repository.save(service)
        await self._service_repository.replace_targets(service.id, set(resolved_target_ids))
        await self._session.commit()
        logger.info(
            "service created",
            extra={
                "action": "create_service",
                "project_id": project.id,
                "service_id": service.id,
                "repository_analysis_id": analysis_id,
            },
        )
        return ServiceDetail(service, resolved_target_ids)

    async def create_analyzed_services(
        self,
        owner_id: int,
        project_id: int,
        repository_url: str,
        branch: str,
        plans: list[AnalyzedServicePlan],
        target_ids: list[int] | None,
        *,
        is_auto_deploy: bool = True,
        stack_id: int | None = None,
        existing_names: set[str] | None = None,
    ) -> list[Service]:
        """레포 구성 분석의 배포 단위마다 서비스를 만든다. 이름·브랜치·타깃 규칙은 create_service 와
        같다.

        커밋하지 않는다. 호출한 쪽이 분석 상태 변경과 같은 트랜잭션으로 커밋해, 일부만 만들어지는
        일이 없게 한다.
        """
        project = await self._get_project(owner_id, project_id)
        repository = await self._source_repository_service.resolve_repository(
            owner_id, repository_url
        )
        await self._ensure_branch_exists(owner_id, repository.full_name, branch)
        names: set[str] = set(existing_names or ())
        for plan in plans:
            if plan.name in names:
                raise ServiceNameConflictError(
                    "service name already exists", service_name=plan.name
                )
            names.add(plan.name)
            await self._ensure_name_available(project.id, plan.name)
        resolved_target_ids = await self._resolve_target_ids(target_ids)
        installation_row_id = await self._get_installation_row_id(repository)

        services: list[Service] = []
        for plan in plans:
            service = await self._service_repository.save(
                Service(
                    project_id=project.id,
                    name=plan.name,
                    source_repository_url=repository.url,
                    github_installation_id=installation_row_id,
                    source_branch=branch,
                    root_directory=plan.root_directory,
                    is_auto_deploy=is_auto_deploy,
                    builder=plan.builder,
                    dockerfile_path=plan.dockerfile_path,
                    port=plan.port,
                    start_command=plan.start_command,
                    build_command=plan.build_command,
                    analysis_plan=plan.analysis_plan,
                    stack_id=stack_id,
                    stack_unit_id=plan.stack_unit_id if stack_id is not None else None,
                )
            )
            await self._service_repository.replace_targets(service.id, set(resolved_target_ids))
            services.append(service)
        return services

    async def search_services_by_ids(
        self, owner_id: int, service_ids: list[int]
    ) -> list[ServiceDetail]:
        """소유자가 볼 수 있는 서비스만 id 순서대로 돌려준다. 지운 서비스는 빠진다."""
        services: list[Service] = []
        for service_id in service_ids:
            service = await self._service_repository.find_by_id_and_owner_id(service_id, owner_id)
            if service is not None:
                services.append(service)
        return await self._detail(services)

    async def get_service(self, owner_id: int, service_id: int) -> ServiceDetail:
        service = await self._get_owned(owner_id, service_id)
        return (await self._detail([service]))[0]

    async def search_services(self, owner_id: int, project_id: int) -> list[ServiceDetail]:
        project = await self._project_repository.find_by_id_and_owner_id(project_id, owner_id)
        if project is None:
            raise ProjectNotFoundError("project not found", project_id=project_id)
        services = await self._service_repository.search_by_project_id(project.id)
        return await self._detail(services)

    async def update_service(
        self, owner_id: int, service_id: int, changes: Mapping[str, Any]
    ) -> ServiceDetail:
        """`changes` 에 있는 키만 바꾼다. 명시한 null 은 값을 비운다(비울 수 없는 필드는 거부).

        배포 방식은 저장만 하고 배포를 만들지 않는다. 다음 배포 요청부터 적용된다.
        """
        # 배포 방식은 저장된 Pod 수로 검사하므로 Pod 수 변경과 겹치지 않게 행을 잠근다.
        service = await self._get_owned(
            owner_id, service_id, for_update="deployment_strategy" in changes
        )
        for field in changes:
            if field not in _NULLABLE_FIELDS | _NON_NULL_FIELDS | _NETWORKING_FIELDS:
                raise InvalidInputError("field cannot be updated", field=field)
            if service.kind == ServiceKind.DATABASE and field in _SOURCE_FIELDS:
                raise InvalidInputError("database services have no source settings", field=field)
            if field in _NON_NULL_FIELDS and changes[field] is None:
                raise InvalidInputError("field cannot be null", field=field)

        if "name" in changes and changes["name"] != service.name:
            await self._ensure_name_available(service.project_id, changes["name"])
        if "source_branch" in changes and changes["source_branch"] != service.source_branch:
            full_name = _full_name(service.source_repository_url)
            await self._ensure_branch_exists(owner_id, full_name, changes["source_branch"])
        if "deployment_strategy" in changes:
            strategy = DeploymentStrategy(changes["deployment_strategy"])
            if strategy != service.deployment_strategy:
                await self._check_deployment_strategy(service, strategy, changes.get("target_ids"))

        if "host_aliases" in changes:
            service.host_aliases = await self._check_host_aliases(
                service, changes["host_aliases"] or []
            )
        for field, value in changes.items():
            if field in ("target_ids", "host_aliases"):
                continue
            if field == "root_directory":
                value = normalize_root_directory(value)
            if field == "builder" and value is not None:
                value = Builder(value)
            if field == "deployment_strategy":
                value = DeploymentStrategy(value)
            setattr(service, field, value)

        if "target_ids" in changes:
            target_ids = await self._resolve_target_ids(changes["target_ids"])
            await self._ensure_target_kept_after_deploy(service.id, target_ids)
            await self._service_repository.replace_targets(service.id, set(target_ids))
        await self._session.commit()
        return (await self._detail([service]))[0]

    async def delete_service(self, owner_id: int, service_id: int) -> None:
        """소프트 삭제하고 떠 있는 앱도 내린다(REMOVE 요청). 진행 중인 배포가 있으면 안 지운다."""
        service = await self._get_owned(owner_id, service_id)
        await self._service_teardown_service.request_teardown([service], owner_id)
        service.mark_as_deleted()
        await self._session.commit()
        logger.info("service deleted", extra={"action": "delete_service", "service_id": service_id})

    async def get_project(self, owner_id: int, project_id: int) -> Project:
        return await self._get_project(owner_id, project_id)

    async def ensure_name_available(self, project_id: int, name: str) -> None:
        await self._ensure_name_available(project_id, name)

    async def resolve_targets(self, target_ids: list[int] | None) -> tuple[list[int], TargetKind]:
        """서비스가 배포될 타깃 id(1개)와 그 종류."""
        resolved = await self._resolve_target_ids(target_ids)
        targets = await self._target_repository.search_by_ids(resolved)
        return resolved, targets[0].kind if targets else TargetKind.AWS

    async def _get_project(self, owner_id: int, project_id: int) -> Project:
        project = await self._project_repository.find_by_id_and_owner_id(project_id, owner_id)
        if project is None:
            raise ProjectNotFoundError("project not found", project_id=project_id)
        return project

    async def _get_installation_row_id(self, repository: RepositoryCandidate) -> int:
        installation = await self._installation_repository.find_by_installation_id(
            repository.installation_id
        )
        if installation is None:
            raise ServiceNotFoundError("github installation not found")
        return installation.id

    async def _get_gate_analysis(
        self,
        project_id: int,
        analysis_id: int,
        repository: RepositoryCandidate,
        root_directory: str | None,
    ) -> RepositoryAnalysis:
        """서비스 생성에 붙일 분석. 같은 프로젝트·저장소·위치를 분석해 끝난 것이어야 한다."""
        if self._repository_analysis_repository is None:
            raise RepositoryAnalysisNotFoundError(
                "repository analysis not found", analysis_id=analysis_id
            )
        analysis = await self._repository_analysis_repository.find_by_id_and_project_id(
            analysis_id, project_id
        )
        if analysis is None:
            raise RepositoryAnalysisNotFoundError(
                "repository analysis not found", analysis_id=analysis_id
            )
        if analysis.status != RepositoryAnalysisStatus.SUCCEEDED:
            raise RepositoryAnalysisNotReadyError(
                "repository analysis is not succeeded",
                analysis_id=analysis_id,
                status=analysis.status,
            )
        is_same_source = (
            analysis.source_repository_url.lower() == repository.url.lower()
            and analysis.root_directory == root_directory
        )
        if not is_same_source:
            raise InvalidInputError(
                "repository analysis is for another repository or root directory",
                issues=[FieldIssue("analysisId", "analysis_source_mismatch")],
                field="analysisId",
                analysis_id=analysis_id,
            )
        return analysis

    async def _get_owned(
        self, owner_id: int, service_id: int, *, for_update: bool = False
    ) -> Service:
        service = await self._service_repository.find_by_id_and_owner_id(
            service_id, owner_id, for_update=for_update
        )
        if service is None:
            raise ServiceNotFoundError("service not found", service_id=service_id)
        return service

    async def _check_deployment_strategy(
        self, service: Service, strategy: DeploymentStrategy, target_ids: list[int] | None
    ) -> None:
        """카나리·블루그린은 기능이 켜져 있고 AWS 타깃이고 Pod 수가 2 이상일 때만 저장한다.

        Pod 수는 저장된 값(없으면 1)이다. on-prem 타깃은 chart 0.6.0 에 남아 롤링만 쓴다.
        같은 요청이 타깃을 바꾸면 바뀐 타깃으로 본다.
        """
        if strategy not in PROGRESSIVE_STRATEGIES:
            return
        if not self._deployment_strategy_enabled:
            reason = "deployment_strategy_disabled"
        elif await self._find_target_kind(service.id, target_ids) not in PROGRESSIVE_TARGET_KINDS:
            reason = "deployment_strategy_unsupported_target"
        elif _desired_replicas(service) < MIN_PROGRESSIVE_REPLICAS:
            reason = "at_least_two_replicas_required"
        else:
            return
        raise InvalidInputError(
            "deployment strategy is not available",
            issues=[FieldIssue("deploymentStrategy", reason)],
            field="deploymentStrategy",
            service_id=service.id,
        )

    async def _find_target_kind(
        self, service_id: int, target_ids: list[int] | None
    ) -> TargetKind | None:
        """서비스가 배포될 타깃의 종류. 타깃이 없는 서비스는 `aws` 에 배포하므로 AWS 다."""
        if target_ids is None:
            target_ids = (
                await self._service_repository.search_target_ids_by_service_ids([service_id])
            )[service_id]
        if not target_ids:
            return TargetKind.AWS
        targets = await self._target_repository.search_by_ids(target_ids)
        return targets[0].kind if targets else None

    async def _ensure_name_available(self, project_id: int, name: str) -> None:
        if not SERVICE_NAME_PATTERN.match(name):
            raise InvalidInputError(
                "service name must be lowercase letters, digits and hyphens", field="name"
            )
        if await self._service_repository.find_by_project_id_and_name(project_id, name):
            raise ServiceNameConflictError("service name already exists", service_name=name)

    async def _ensure_branch_exists(self, owner_id: int, full_name: str, branch: str) -> None:
        branches = await self._source_repository_service.search_branches(owner_id, full_name)
        if branch not in {b.name for b in branches}:
            raise InvalidInputError("branch not found in repository", field="branch", branch=branch)

    async def _resolve_target_ids(self, target_ids: list[int] | None) -> list[int]:
        """서비스는 타깃 하나에만 배포한다. 지정이 없으면 `aws` 타깃이다."""
        if target_ids is None:
            targets = await self._target_repository.search_all()
            return [t.id for t in targets if t.name == AWS_TARGET_NAME]
        wanted = sorted(set(target_ids))
        if len(wanted) != 1:
            raise InvalidInputError("exactly one target is required", field="targetIds")
        if not await self._target_repository.search_by_ids(wanted):
            raise InvalidInputError("unknown target", field="targetIds", target_ids=wanted)
        return wanted

    async def _ensure_target_kept_after_deploy(
        self, service_id: int, target_ids: list[int]
    ) -> None:
        """배포한 뒤 타깃을 바꾸면 두 ApplicationSet 이 같은 svc-{id} 를 만든다."""
        current = (await self._service_repository.search_target_ids_by_service_ids([service_id]))[
            service_id
        ]
        if current == target_ids:
            return
        if await self._deployment_request_repository.count_by_service_id(service_id):
            raise ConflictError("target cannot change after deployment", field="targetIds")

    async def _check_host_aliases(
        self, service: Service, aliases: list[dict[str, Any]]
    ) -> list[dict[str, Any]] | None:
        """별칭은 같은 프로젝트의 다른 서비스로만, 서비스 사이 통신이 되는 타깃에서만 둔다."""
        if not aliases:
            return None
        if not is_networking_available(
            self._is_networking_enabled, await self._find_target_kind(service.id, None)
        ):
            raise InvalidInputError(
                "host aliases are not available for this target",
                issues=[
                    FieldIssue(
                        "hostAliases", networking_unavailable_reason(self._is_networking_enabled)
                    )
                ],
                field="hostAliases",
                service_id=service.id,
            )
        project_services = {
            s.id: s for s in await self._service_repository.search_by_project_id(service.project_id)
        }
        return validate_host_aliases(service, aliases, project_services)

    async def _detail(self, services: list[Service]) -> list[ServiceDetail]:
        target_ids = await self._service_repository.search_target_ids_by_service_ids(
            [s.id for s in services]
        )
        latest = await self._deployment_request_repository.search_latest_by_service_ids(
            [s.id for s in services]
        )
        kinds: dict[int, TargetKind] = {}
        if self._is_networking_enabled:
            all_target_ids = sorted({t for ids in target_ids.values() for t in ids})
            kinds = {
                t.id: t.kind for t in await self._target_repository.search_by_ids(all_target_ids)
            }
        details = []
        for s in services:
            ids = target_ids.get(s.id, [])
            kind = kinds.get(ids[0]) if ids else TargetKind.AWS
            details.append(
                ServiceDetail(
                    s,
                    ids,
                    latest.get(s.id),
                    is_networking=is_networking_available(self._is_networking_enabled, kind),
                )
            )
        return details


def _apply_gate_defaults(service: Service, analysis: RepositoryAnalysis) -> None:
    """분석 결정을 남기고, 생략(skip) 결정이면 분석기가 고른 빌더를 기본값으로 쓴다."""
    service.analysis_plan = {"gate": analysis.build_gate_plan(unit_id=None)}
    if analysis.decision != AnalysisGateDecision.SKIP or analysis.result is None:
        return
    simple_build = AnalysisGateResult.model_validate(analysis.result).simple_build
    if simple_build is None:
        return
    service.builder = simple_build.builder
    if simple_build.builder == Builder.DOCKERFILE:
        service.dockerfile_path = simple_build.dockerfile_path


def _desired_replicas(service: Service) -> int:
    if service.scaling_config is None:
        return ScalingConfig.defaults().replicas
    return ScalingConfig.model_validate(service.scaling_config).replicas


def _full_name(repository_url: str) -> str:
    return repository_url.removeprefix("https://github.com/")
