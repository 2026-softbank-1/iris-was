"""분석 결과를 스택으로 적용한다: DB 서비스 → 앱 서비스 → 참조 변수 → 호스트 별칭.

같은 프로젝트·저장소·브랜치·위치의 스택이 이미 있으면 증분 적용이다. 서비스는 분석기 unit·dependency
id(`stack_unit_id`)로 맞춰 다시 만들지 않고 분석 값으로 고친다. 새 unit·의존성만 만든다. 사라진
unit 의 서비스는 지우지 않는다(스택 `pendingChanges` 에 UNIT_REMOVED 로만 보인다).

참조 변수는 같은 키가 없을 때 만들고, 이미 참조면 새 대상으로 고친다. 사용자가 값으로 바꾼 변수는
그대로 둔다. 별칭은 같은 이름만 바꾸고 사용자가 더한 별칭은 남긴다. 모두 호출한 쪽 트랜잭션 안이다.
"""

import logging
import secrets as secrets_module
from dataclasses import dataclass, field
from typing import Any

from app.core.crypto import VariableCipher
from app.core.exceptions import FieldIssue, InvalidInputError, NotConfiguredError
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
from app.schemas.analysis_gate import (
    AnalysisGateBinding,
    AnalysisGateDependency,
    AnalysisGateResult,
    AnalysisGateSecret,
    AnalysisGateUnit,
)
from app.services.database_init_scripts import (
    init_scripts_change,
    init_scripts_fingerprint,
    select_init_scripts,
)
from app.services.database_service import DatabasePlan, DatabaseService, check_networking
from app.services.service_networking import (
    is_networking_available,
    is_valid_alias_name,
    supported_properties,
)
from app.services.service_registry_service import (
    AnalyzedServicePlan,
    ServiceRegistryService,
    slugify_service_name,
)
from app.services.variable_references import reference_from_parts
from app.services.variable_service import check_variable, managed_variable_keys

logger = logging.getLogger(__name__)

_ENGINES = frozenset(engine.value for engine in DatabaseEngine)
_APP_PROPERTIES = frozenset({ReferenceProperty.URL, ReferenceProperty.HOST, ReferenceProperty.PORT})
# 생성 비밀값은 앱의 최소 길이 검사(예: SESSION_SECRET >= 48자)를 넘도록
# 64자(48바이트 URL-safe)로 만든다.
_SECRET_BYTES = 48


@dataclass(frozen=True)
class GeneratedSecret:
    """이번 apply 가 만든(또는 이미 있던 값으로 채운) 공유 비밀값. 값은 담지 않는다."""

    id: str
    service_ids: list[int]


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
    generated_secrets: list[GeneratedSecret] = field(default_factory=list)


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
        cipher: VariableCipher | None = None,
    ) -> None:
        self._cipher = cipher
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
            owner_id, target_ids
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

        # 3) 비밀값: 생성한 값은 모든 consumer(앱·DB)에 같은 값, 플랫폼 관리 값은 DB 참조.
        for secret in result.secrets:
            generated = await self._apply_secret(result, secret, existing, networking)
            if generated is not None:
                applied.generated_secrets.append(generated)

        # 4) 참조 변수·호스트 별칭 (서비스 사이 통신이 되는 타깃에서만)
        if networking:
            for service in applied.apps:
                unit = result.find_unit(service.stack_unit_id or "")
                if unit is None:
                    continue
                await self._link_references(service, unit, existing, result)
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

    async def _apply_secret(
        self,
        result: AnalysisGateResult,
        secret: AnalysisGateSecret,
        by_unit: dict[str, Service],
        networking: bool,
    ) -> GeneratedSecret | None:
        """이미 있는 변수는 덮어쓰지 않는다(증분 apply 멱등). 값은 응답·로그에 남기지 않는다."""
        consumers: list[tuple[Service, str]] = []
        for target_id, key in _secret_consumers(result, secret, networking=networking):
            service = by_unit.get(target_id)
            if service is None or check_variable(key, "") is not None:
                continue
            if key in managed_variable_keys(service):
                # DB 엔진 관리 키는 플랫폼 자격 증명이 채운다.
                continue
            if (service.id, key) not in {(s.id, k) for s, k in consumers}:
                consumers.append((service, key))
        if not consumers:
            return None
        if secret.platform_managed is not None:
            await self._link_platform_managed(secret, consumers, by_unit, networking)
            return None
        if secret.generate != "random":
            return None
        if self._cipher is None:
            raise NotConfiguredError(
                "variables encryption is not configured", setting="VARIABLES_ENCRYPTION_KEY"
            )
        # 어느 consumer 에 이미 값이 있으면(이전 apply·사용자 입력) 그 값을 나눠 쓴다.
        value: str | None = None
        for service, key in consumers:
            variable = await self._service_variable_repository.find_by_service_id_and_key(
                service.id, key
            )
            if variable is not None and variable.encrypted_value is not None:
                value = self._cipher.decrypt(variable.encrypted_value)
                break
        if value is None:
            value = secrets_module.token_urlsafe(_SECRET_BYTES)
        written: list[int] = []
        for service, key in consumers:
            created = await self._service_variable_repository.add_if_absent(
                service.id, key, self._cipher.encrypt(value)
            )
            if created is not None and service.id not in written:
                written.append(service.id)
        if not written:
            return None
        return GeneratedSecret(secret.id, written)

    async def _link_platform_managed(
        self,
        secret: AnalysisGateSecret,
        consumers: list[tuple[Service, str]],
        by_unit: dict[str, Service],
        networking: bool,
    ) -> None:
        """DB 엔진 관리 키에 매핑된 비밀값은 그 DB 의 속성(password)을 참조로 받는다."""
        assert secret.platform_managed is not None
        database = by_unit.get(secret.platform_managed.dependency_id)
        if not networking or database is None or database.kind != ServiceKind.DATABASE:
            return
        try:
            prop = ReferenceProperty(secret.platform_managed.property)
        except ValueError:
            return
        if prop not in supported_properties(database):
            return
        for service, key in consumers:
            if service.id == database.id:
                continue
            await self._service_variable_repository.add_if_absent(
                service.id, key, None, {"serviceId": database.id, "property": prop.value}
            )

    async def _link_references(
        self,
        service: Service,
        unit: AnalysisGateUnit,
        by_unit: dict[str, Service],
        result: AnalysisGateResult,
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
            password_variable = (
                secret_key_for(result, binding.password_secret_id, unit.id)
                if binding.password_secret_id and target.kind == ServiceKind.DATABASE
                else None
            )
            reference = _binding_reference(target.id, prop, binding, password_variable)
            variable = await self._service_variable_repository.find_by_service_id_and_key(
                service.id, env.key
            )
            if variable is None:
                await self._service_variable_repository.add_if_absent(
                    service.id, env.key, None, reference
                )
            elif variable.reference is not None:
                variable.reference = reference


def _binding_reference(
    target_id: int,
    prop: ReferenceProperty,
    binding: AnalysisGateBinding,
    password_variable: str | None = None,
) -> dict[str, Any]:
    """분석기 binding 을 참조로. url 이면 코드가 쓴 스킴·경로·쿼리와 사용자(userinfo)를 함께 옮긴다.
    받을 수 없는 값은 버린다(기본 스킴·접미사 없음, DB 관리 자격 증명)."""
    if prop != ReferenceProperty.URL:
        return {"serviceId": target_id, "property": prop.value}
    # 코드의 사용자는 비밀값(passwordSecretId)과 함께일 때만 쓴다(초기화 스크립트가 만든 앱 사용자).
    # 비밀번호가 리터럴이면 플랫폼이 만든 사용자·비밀번호로 바꾼다(이전과 같다).
    return reference_from_parts(
        target_id,
        prop,
        binding.scheme,
        binding.url_suffix,
        user=binding.user if password_variable else None,
        password_variable=password_variable,
    ).to_json()


def secret_key_for(result: AnalysisGateResult, secret_id: str, target_id: str) -> str:
    """target(unit·dependency) 에서 비밀값을 담는 변수 키. env 로 받는 키가 있으면 그 키, 없으면
    비밀값 id 다(URL 비밀번호로만 쓰는 값)."""
    secret = next((s for s in result.secrets if s.id == secret_id), None)
    if secret is not None:
        for consumer in secret.consumers:
            if consumer.target_id == target_id and consumer.via == "env":
                return consumer.key
    unit = result.find_unit(target_id)
    if unit is not None:
        for env in unit.env:
            if env.secret_id == secret_id:
                return env.key
    return secret_id


def _secret_consumers(
    result: AnalysisGateResult, secret: AnalysisGateSecret, *, networking: bool
) -> list[tuple[str, str]]:
    """(unit·dependency id, 변수 키). consumers 와 units/dependencies env 의 secretId 를 합친다.

    url_password 는 URL 자체가 참조 변수라 비밀값을 `secret_key_for` 키의 변수로 둔다(참조의
    passwordVariable). 참조를 쓸 수 없는 타깃이면 두지 않는다.
    """
    found: list[tuple[str, str]] = []
    for consumer in secret.consumers:
        if consumer.kind not in ("unit", "dependency"):
            continue
        if consumer.via == "env":
            found.append((consumer.target_id, consumer.key))
        elif consumer.via == "url_password" and networking:
            found.append(
                (consumer.target_id, secret_key_for(result, secret.id, consumer.target_id))
            )
    for unit in result.units:
        found.extend((unit.id, env.key) for env in unit.env if env.secret_id == secret.id)
    for dependency in result.dependencies:
        found.extend(
            (dependency.id, env.key) for env in dependency.env if env.secret_id == secret.id
        )
    return found


def _update_from_plan(service: Service, plan: AnalyzedServicePlan) -> None:
    """증분 apply: 이미 있는 unit 의 서비스 설정을 새 분석 값으로 맞춘다(이름은 그대로)."""
    service.root_directory = plan.root_directory
    service.builder = plan.builder
    service.dockerfile_path = plan.dockerfile_path
    service.docker_target = plan.docker_target
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
