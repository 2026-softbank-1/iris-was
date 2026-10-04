"""분석 결과를 스택으로 적용한다: DB 서비스 → 앱 서비스 → 참조 변수 → 호스트 별칭.

같은 프로젝트·저장소·브랜치·위치의 스택이 이미 있으면 증분 적용이다. 서비스는 분석기 unit·dependency
id(`stack_unit_id`)로 맞춰 다시 만들지 않고 분석 값으로 고친다. 새 unit·의존성만 만든다. 사라진
unit 의 서비스는 지우지 않는다(스택 `pendingChanges` 에 UNIT_REMOVED 로만 보인다).

참조 변수는 같은 키가 없을 때 만들고, 이미 참조면 새 대상으로 고친다. 사용자가 값으로 바꾼 변수는
그대로 둔다. 별칭은 같은 이름만 바꾸고 사용자가 더한 별칭은 남긴다. 모두 호출한 쪽 트랜잭션 안이다.
"""

import logging
from dataclasses import dataclass, field
from typing import Any

from app.core.exceptions import FieldIssue, InvalidInputError
from app.enums import (
    DatabaseEngine,
    ReferenceProperty,
    ServiceKind,
    TargetKind,
)
from app.models.repository_analysis import RepositoryAnalysis
from app.models.service import Service
from app.models.service_stack import ServiceStack
from app.repositories.database_init_script_repository import DatabaseInitScriptRepository
from app.repositories.service_repository import ServiceRepository
from app.repositories.service_stack_repository import ServiceStackRepository
from app.repositories.service_variable_repository import ServiceVariableRepository
from app.schemas.analysis_gate import AnalysisGateDependency, AnalysisGateResult, AnalysisGateUnit
from app.services.database_init_scripts import (
    init_scripts_change,
    init_scripts_fingerprint,
    select_init_scripts,
)
from app.services.database_service import DatabasePlan, DatabaseService, check_networking
from app.services.service_networking import is_networking_available, is_valid_alias_name
from app.services.service_registry_service import (
    AnalyzedServicePlan,
    ServiceRegistryService,
    slugify_service_name,
)
from app.services.variable_service import check_variable

logger = logging.getLogger(__name__)

_ENGINES = frozenset(engine.value for engine in DatabaseEngine)
_APP_PROPERTIES = frozenset({ReferenceProperty.URL, ReferenceProperty.HOST, ReferenceProperty.PORT})


@dataclass(frozen=True)
class DependencySelection:
    """apply 에서 분석된 의존성(DB)을 플랫폼이 만들지. 생략한 의존성은 기본값(지원 엔진이면
    만든다)."""

    dependency_id: str
    is_provisioned: bool = True
    name: str | None = None
    storage_gi: int | None = None


@dataclass
class AppliedStack:
    stack: ServiceStack
    apps: list[Service] = field(default_factory=list)
    databases: list[Service] = field(default_factory=list)
    # 이번 apply 로 새로 만든 DB(첫 배포 대상).
    created_databases: list[Service] = field(default_factory=list)
    # 이미 있는 DB 의 초기화 스크립트가 분석과 달라진 것(DEPENDENCY_CHANGED). DB 는 그대로 둔다.
    changes: list[dict[str, Any]] = field(default_factory=list)


class StackApplyService:
    def __init__(
        self,
        service_registry_service: ServiceRegistryService,
        database_service: DatabaseService,
        service_repository: ServiceRepository,
        service_variable_repository: ServiceVariableRepository,
        stack_repository: ServiceStackRepository,
        init_script_repository: DatabaseInitScriptRepository,
        *,
        is_networking_enabled: bool,
    ) -> None:
        self._init_script_repository = init_script_repository
        self._service_registry_service = service_registry_service
        self._database_service = database_service
        self._service_repository = service_repository
        self._service_variable_repository = service_variable_repository
        self._stack_repository = stack_repository
        self._is_networking_enabled = is_networking_enabled

    async def apply(
        self,
        owner_id: int,
        analysis: RepositoryAnalysis,
        plans: list[AnalyzedServicePlan],
        dependency_selections: list[DependencySelection] | None,
        target_ids: list[int] | None,
        *,
        is_auto_deploy: bool,
    ) -> AppliedStack:
        result = AnalysisGateResult.model_validate(analysis.result or {})
        project = await self._service_registry_service.get_project(owner_id, analysis.project_id)
        resolved_target_ids, target_kind = await self._service_registry_service.resolve_targets(
            target_ids
        )
        networking = is_networking_available(self._is_networking_enabled, target_kind)
        stack = await self._find_or_create_stack(analysis)
        existing = {
            s.stack_unit_id: s
            for s in await self._service_repository.search_by_stack_id(stack.id)
            if s.stack_unit_id
        }
        applied = AppliedStack(stack)

        # 1) DB 서비스
        for dependency_id, selection in self._provisioned(
            result, dependency_selections, networking, target_kind
        ):
            dependency = result.find_dependency(dependency_id)
            assert dependency is not None
            service = existing.get(dependency_id)
            if service is not None and service.kind != ServiceKind.DATABASE:
                # 같은 id 의 앱이 있다(분석이 의존성을 unit 으로 바꿨다). 만들지 않는다.
                continue
            init_scripts = await self._init_scripts(dependency)
            if service is None:
                service = await self._database_service.add_database(
                    project,
                    DatabasePlan(
                        name=selection.name or slugify_service_name(dependency.id),
                        engine=DatabaseEngine(dependency.engine),
                        storage_gi=selection.storage_gi or 5,
                        user=dependency.user,
                        database=dependency.database,
                        stack_id=stack.id,
                        stack_unit_id=dependency.id,
                        init_scripts=init_scripts,
                    ),
                    resolved_target_ids,
                )
                applied.created_databases.append(service)
                existing[dependency.id] = service
            else:
                # 이미 초기화된 DB 에는 스크립트를 다시 실행하지 않는다. 바뀐 것만 알린다.
                change = init_scripts_change(
                    dependency.id,
                    init_scripts_fingerprint((service.database_config or {}).get("initScripts")),
                    init_scripts_fingerprint(init_scripts),
                    service_id=service.id,
                )
                if change is not None:
                    applied.changes.append(change)
            applied.databases.append(service)

        # 2) 앱 서비스: 있는 unit 은 고치고 새 unit 만 만든다.
        new_plans: list[AnalyzedServicePlan] = []
        for plan in plans:
            service = existing.get(plan.stack_unit_id or "")
            if service is None:
                new_plans.append(plan)
                continue
            if service.kind != ServiceKind.APP:
                raise InvalidInputError(
                    "unit id is used by a database service in this stack",
                    issues=[FieldIssue("units", "unit_conflicts_with_database")],
                    field="units",
                    unit_id=plan.stack_unit_id,
                )
            _update_from_plan(service, plan)
            applied.apps.append(service)
        created = await self._service_registry_service.create_analyzed_services(
            owner_id,
            analysis.project_id,
            analysis.source_repository_url,
            analysis.source_branch,
            new_plans,
            target_ids,
            is_auto_deploy=is_auto_deploy,
            stack_id=stack.id,
        )
        for service in created:
            existing[service.stack_unit_id or ""] = service
        applied.apps.extend(created)

        # 3) 참조 변수·호스트 별칭 (서비스 사이 통신이 되는 타깃에서만)
        if networking:
            for service in applied.apps:
                unit = result.find_unit(service.stack_unit_id or "")
                if unit is None:
                    continue
                await self._link_references(service, unit, existing)
                _merge_aliases(service, unit, existing)
        stack.rebase(analysis.id)
        await self._service_repository.flush()
        return applied

    def _provisioned(
        self,
        result: AnalysisGateResult,
        selections: list[DependencySelection] | None,
        networking: bool,
        target_kind: TargetKind,
    ) -> list[tuple[str, DependencySelection]]:
        """만들 의존성. 명시하지 않으면 기능을 쓸 수 있을 때 지원 엔진 의존성을 모두 만든다."""
        by_id = {s.dependency_id: s for s in selections or []}
        for dependency_id in by_id:
            if result.find_dependency(dependency_id) is None:
                raise InvalidInputError(
                    "dependency not found in analysis",
                    issues=[FieldIssue("dependencies", "dependency_not_found")],
                    field="dependencies",
                    dependency_id=dependency_id,
                )
        chosen: list[tuple[str, DependencySelection]] = []
        for dependency in result.dependencies:
            selection = by_id.get(dependency.id)
            if selection is None:
                if not networking or dependency.engine not in _ENGINES:
                    continue
                selection = DependencySelection(dependency.id)
            if not selection.is_provisioned:
                continue
            if dependency.engine not in _ENGINES:
                raise InvalidInputError(
                    "dependency engine is not supported",
                    issues=[FieldIssue("dependencies", "unsupported_engine")],
                    field="dependencies",
                    dependency_id=dependency.id,
                )
            check_networking(self._is_networking_enabled, target_kind, "dependencies")
            chosen.append((dependency.id, selection))
        return chosen

    async def _init_scripts(self, dependency: AnalysisGateDependency) -> list[dict[str, Any]]:
        """Build Worker 가 확인·저장한 초기화 스크립트만 DB 서비스로 옮긴다(메타데이터)."""
        sha256s = {s.sha256 for s in dependency.init_scripts if s.supported and s.sha256}
        stored = await self._init_script_repository.search_existing_sha256s(sha256s)
        return select_init_scripts(dependency.engine, dependency.init_scripts, stored)

    async def _find_or_create_stack(self, analysis: RepositoryAnalysis) -> ServiceStack:
        if analysis.stack_id is not None:
            return await self._stack_repository.get_by_id(analysis.stack_id, for_update=True)
        stack = await self._stack_repository.find_by_source(
            analysis.project_id,
            analysis.source_repository_url,
            analysis.source_branch,
            analysis.root_directory,
        )
        if stack is not None:
            return stack
        return await self._stack_repository.save(
            ServiceStack(
                project_id=analysis.project_id,
                source_repository_url=analysis.source_repository_url,
                source_branch=analysis.source_branch,
                root_directory=analysis.root_directory,
                github_installation_id=analysis.github_installation_id,
                analysis_id=analysis.id,
            )
        )

    async def _link_references(
        self, service: Service, unit: AnalysisGateUnit, by_unit: dict[str, Service]
    ) -> None:
        for env in unit.env:
            binding = env.binding
            if binding is None or check_variable(env.key, "") is not None:
                continue
            target = by_unit.get(binding.target_id)
            if target is None or target.id == service.id:
                continue
            try:
                prop = ReferenceProperty(binding.property)
            except ValueError:
                continue
            if target.kind == ServiceKind.APP and prop not in _APP_PROPERTIES:
                continue
            reference = {"serviceId": target.id, "property": prop.value}
            variable = await self._service_variable_repository.find_by_service_id_and_key(
                service.id, env.key
            )
            if variable is None:
                await self._service_variable_repository.add_if_absent(
                    service.id, env.key, None, reference
                )
            elif variable.reference is not None:
                variable.reference = reference


def _update_from_plan(service: Service, plan: AnalyzedServicePlan) -> None:
    """증분 apply: 이미 있는 unit 의 서비스 설정을 새 분석 값으로 맞춘다(이름은 그대로)."""
    service.root_directory = plan.root_directory
    service.builder = plan.builder
    service.dockerfile_path = plan.dockerfile_path
    service.port = plan.port
    service.start_command = plan.start_command
    service.build_command = plan.build_command
    service.analysis_plan = plan.analysis_plan


def _merge_aliases(service: Service, unit: AnalysisGateUnit, by_unit: dict[str, Service]) -> None:
    aliases: dict[str, dict[str, Any]] = {
        str(a.get("name")): dict(a) for a in service.host_aliases or []
    }
    for alias in unit.host_aliases:
        target = by_unit.get(alias.target_id)
        if target is None or target.id == service.id or not is_valid_alias_name(alias.host):
            continue
        entry: dict[str, Any] = {"name": alias.host, "targetServiceId": target.id}
        if alias.port is not None:
            entry["port"] = alias.port
        aliases[alias.host] = entry
    service.host_aliases = list(aliases.values()) or None
