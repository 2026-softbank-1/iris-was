"""배포 전 환경변수 검증. 컨테이너 안에서 확실히 실패할 설정을 배포 요청 전에 잡는다.

- REQUIRED_MISSING(error): 분석기가 runtime 필수라고 본 키가 없다. 분석으로 만든 서비스만 본다.
- LOCALHOST_ADDRESS(error): 연결 주소 성격의 키(`*_URL`·`*_URI`·`*_HOST`·`*_ADDR`)가 localhost 다.
  컨테이너의 localhost 에는 다른 서비스가 없다. `BIND`·`LISTEN` 이 들어간 키(listen 주소)는 보지
  않는다.
- UNRESOLVABLE_HOST(error): 점이 없는 호스트명인데 이 서비스의 호스트 별칭이 아니다. 클러스터 DNS 가
  풀지 못한다(외부 도메인·FQDN·IP 는 통과).
- SCHEME_MISMATCH(warning): URL 스킴이 키 이름이나 가리키는 DB 엔진과 맞지 않는다.
- REFERENCE_BROKEN(error): 참조 변수의 대상이 지워졌거나 다른 프로젝트다.

error 가 하나라도 있으면 배포 요청은 422 `VARIABLES_INVALID` 다. 값은 이슈에 담지 않는다.
"""

import ipaddress
import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from app.core.crypto import VariableCipher
from app.core.exceptions import (
    FieldIssue,
    ServiceNotFoundError,
    VariableDecryptionError,
    VariableReferenceBrokenError,
    VariablesInvalidError,
)
from app.enums import (
    DatabaseEngine,
    ReferenceProperty,
    ServiceKind,
    VariableIssueCode,
    VariableIssueSeverity,
)
from app.models.service import Service
from app.models.service_variable import ServiceVariable
from app.repositories.service_repository import ServiceRepository
from app.repositories.service_variable_repository import ServiceVariableRepository
from app.services.database_engines import ENGINE_SPECS, get_engine_spec
from app.services.variable_references import ReferenceResolver, VariableReference

logger = logging.getLogger(__name__)

_ADDRESS_KEY = re.compile(r"(^|_)(URL|URI|HOST|ADDR|ADDRESS)$")
_LISTEN_KEY = re.compile(r"(^|_)(BIND|LISTEN)(_|$)")
_LOCALHOST_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})
# 키 이름으로 짐작하는 DB 엔진. DATABASE_URL 은 엔진을 알 수 없어 SCHEME 검사는 하지 않고 제안만
# 한다.
_ENGINE_BY_KEY_PREFIX: tuple[tuple[str, DatabaseEngine], ...] = (
    ("POSTGRES", DatabaseEngine.POSTGRES),
    ("POSTGRESQL", DatabaseEngine.POSTGRES),
    ("PG", DatabaseEngine.POSTGRES),
    ("MYSQL", DatabaseEngine.MYSQL),
    ("MARIADB", DatabaseEngine.MYSQL),
    ("MONGO", DatabaseEngine.MONGODB),
    ("MONGODB", DatabaseEngine.MONGODB),
    ("REDIS", DatabaseEngine.REDIS),
)
_GENERIC_DATABASE_KEYS = frozenset({"DATABASE_URL", "DB_URL", "DATABASE_URI", "DB_URI"})
_SYSTEM_KEYS = frozenset({"PORT"})
_SYSTEM_PREFIX = "IRIS_"


@dataclass(frozen=True)
class VariableIssue:
    key: str
    severity: VariableIssueSeverity
    code: VariableIssueCode
    message: str
    suggestion: VariableReference | None = None

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "key": self.key,
            "severity": self.severity.value,
            "code": self.code.value,
            "message": self.message,
        }
        if self.suggestion is not None:
            data["suggestion"] = {"reference": self.suggestion.to_json()}
        return data


@dataclass(frozen=True)
class VariableValidation:
    service_id: int
    issues: list[VariableIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(issue.severity == VariableIssueSeverity.ERROR for issue in self.issues)

    @property
    def errors(self) -> list[VariableIssue]:
        return [i for i in self.issues if i.severity == VariableIssueSeverity.ERROR]

    def to_json(self) -> dict[str, Any]:
        return {"ok": self.ok, "issues": [issue.to_json() for issue in self.issues]}


def is_address_key(key: str) -> bool:
    return bool(_ADDRESS_KEY.search(key)) and not _LISTEN_KEY.search(key)


def engine_hint(key: str) -> DatabaseEngine | None:
    """키 이름이 가리키는 DB 엔진(`REDIS_URL` → redis). 모르면 None."""
    first = key.split("_", 1)[0]
    return next((engine for prefix, engine in _ENGINE_BY_KEY_PREFIX if first == prefix), None)


def _is_localhost(host: str) -> bool:
    if host.lower() in _LOCALHOST_NAMES:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


@dataclass(frozen=True)
class _Address:
    scheme: str | None
    host: str


def _parse_address(key: str, value: str) -> _Address | None:
    """값에서 호스트를 꺼낸다. URL 이 아니면 `host[:port]` 로 본다. 호스트가 없으면 None."""
    text = value.strip()
    if not text:
        return None
    if "://" in text:
        try:
            parts = urlsplit(text)
            host = parts.hostname
        except ValueError:
            return None
        if not host:
            return None
        scheme = parts.scheme.lower().split("+", 1)[0] if parts.scheme else None
        return _Address(scheme, host)
    if key.endswith(("_URL", "_URI")) and "/" in text:
        # 상대 경로(`/api`)는 주소가 아니다.
        return None
    if text.startswith("["):
        host = text[1 : text.find("]")] if "]" in text else text
    elif text.count(":") == 1:
        host = text.split(":", 1)[0]
    else:
        host = text
    return _Address(None, host) if host and " " not in host else None


class VariableValidationService:
    """`cipher` 가 없으면 값 검사(localhost·호스트·스킴)는 건너뛰고 필수 키·참조만 본다."""

    def __init__(
        self,
        service_repository: ServiceRepository,
        service_variable_repository: ServiceVariableRepository,
        *,
        is_networking_enabled: bool,
        cipher: VariableCipher | None,
    ) -> None:
        self._service_repository = service_repository
        self._service_variable_repository = service_variable_repository
        self._cipher = cipher
        self._resolver = ReferenceResolver(
            service_repository,
            service_variable_repository,
            is_networking_enabled=is_networking_enabled,
            cipher=cipher,
        )

    async def validate_owned(self, owner_id: int, service_id: int) -> VariableValidation:
        service = await self._service_repository.find_by_id_and_owner_id(service_id, owner_id)
        if service is None:
            raise ServiceNotFoundError("service not found", service_id=service_id)
        return await self.validate(service)

    async def check_deployable(self, services: Iterable[Service]) -> None:
        """error 가 있는 서비스가 하나라도 있으면 VariablesInvalidError. 이슈는 서비스마다
        묶는다."""
        validations = [await self.validate(service) for service in services]
        failed = [v for v in validations if not v.ok]
        if not failed:
            return
        raise_invalid(failed)

    async def validate(self, service: Service) -> VariableValidation:
        if service.kind == ServiceKind.DATABASE:
            # 관리형 DB 의 변수는 플랫폼이 만든 자격 증명뿐이다.
            return VariableValidation(service.id)
        variables = await self._service_variable_repository.search_by_service_id(service.id)
        project_services = await self._service_repository.search_by_project_id(service.project_id)
        context = _Context(service, project_services)
        issues: list[VariableIssue] = []
        issues.extend(self._check_required(service, variables, context))
        for variable in variables:
            if variable.reference is not None:
                issues.extend(await self._check_reference(service, variable))
            elif self._cipher is not None and is_address_key(variable.key):
                issues.extend(self._check_value(variable, context))
        return VariableValidation(service.id, issues)

    def _check_required(
        self, service: Service, variables: list[ServiceVariable], context: "_Context"
    ) -> list[VariableIssue]:
        """분석으로 만든 서비스만 본다. 분석 정보가 없는 기존 서비스는 검사하지 않는다."""
        unit = (service.analysis_plan or {}).get("unit")
        if not isinstance(unit, dict):
            return []
        present = {v.key for v in variables}
        issues: list[VariableIssue] = []
        for env in unit.get("env") or []:
            if not isinstance(env, dict) or not env.get("required"):
                continue
            key = env.get("key")
            if not isinstance(key, str) or key in present or _is_system_key(key):
                continue
            if env.get("stage", "runtime") != "runtime":
                continue
            issues.append(
                VariableIssue(
                    key,
                    VariableIssueSeverity.ERROR,
                    VariableIssueCode.REQUIRED_MISSING,
                    "required variable is missing",
                    context.suggest_for_binding(env.get("binding")) or context.suggest(key),
                )
            )
        return issues

    async def _check_reference(
        self, service: Service, variable: ServiceVariable
    ) -> list[VariableIssue]:
        assert variable.reference is not None
        try:
            await self._resolver.get_target(
                service, VariableReference.from_json(variable.reference)
            )
        except (VariableReferenceBrokenError, ValueError):
            return [
                VariableIssue(
                    variable.key,
                    VariableIssueSeverity.ERROR,
                    VariableIssueCode.REFERENCE_BROKEN,
                    "referenced service no longer exists in this project",
                )
            ]
        return []

    def _check_value(self, variable: ServiceVariable, context: "_Context") -> list[VariableIssue]:
        assert self._cipher is not None and variable.encrypted_value is not None
        try:
            value = self._cipher.decrypt(variable.encrypted_value)
        except VariableDecryptionError:
            return []
        address = _parse_address(variable.key, value)
        if address is None:
            return []
        key = variable.key
        if _is_localhost(address.host):
            return [
                VariableIssue(
                    key,
                    VariableIssueSeverity.ERROR,
                    VariableIssueCode.LOCALHOST_ADDRESS,
                    "localhost inside the container does not reach other services",
                    context.suggest(key, scheme=address.scheme),
                )
            ]
        issues: list[VariableIssue] = []
        target = None
        if "." not in address.host and not _is_ip(address.host):
            target = context.alias_target(address.host)
            if target is None and address.host.lower() != "app":
                issues.append(
                    VariableIssue(
                        key,
                        VariableIssueSeverity.ERROR,
                        VariableIssueCode.UNRESOLVABLE_HOST,
                        "host is not a host alias of this service or a fully qualified name",
                        context.suggest(key, host=address.host, scheme=address.scheme),
                    )
                )
        if address.scheme is not None:
            expected = (
                target.database_engine
                if target is not None and target.database_engine is not None
                else engine_hint(key)
            )
            if expected is not None and address.scheme in _ALL_DATABASE_SCHEMES:
                if address.scheme not in get_engine_spec(expected).accepted_schemes:
                    issues.append(
                        VariableIssue(
                            key,
                            VariableIssueSeverity.WARNING,
                            VariableIssueCode.SCHEME_MISMATCH,
                            f"url scheme does not match {expected.value}",
                            context.suggest(key, engine=expected),
                        )
                    )
        return issues


_ALL_DATABASE_SCHEMES = frozenset(
    scheme for spec in ENGINE_SPECS.values() for scheme in spec.accepted_schemes
)


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _is_system_key(key: str) -> bool:
    return key in _SYSTEM_KEYS or key.startswith(_SYSTEM_PREFIX)


class _Context:
    """제안(suggestion)을 고를 때 보는 같은 프로젝트 서비스들."""

    def __init__(self, service: Service, project_services: list[Service]) -> None:
        self._service = service
        self._others = [s for s in project_services if s.id != service.id]
        by_id = {s.id: s for s in self._others}
        self._alias_targets: dict[str, Service] = {}
        for alias in service.host_aliases or []:
            target = by_id.get(alias.get("targetServiceId"))  # type: ignore[arg-type]
            if target is not None:
                self._alias_targets[str(alias.get("name"))] = target

    def alias_target(self, host: str) -> Service | None:
        return self._alias_targets.get(host)

    def suggest_for_binding(self, binding: object) -> VariableReference | None:
        """분석기 binding(targetId=unit·dependency id)을 같은 스택의 서비스 참조로 바꾼다."""
        if not isinstance(binding, Mapping) or self._service.stack_id is None:
            return None
        target = next(
            (
                s
                for s in self._others
                if s.stack_id == self._service.stack_id
                and s.stack_unit_id == binding.get("targetId")
            ),
            None,
        )
        try:
            prop = ReferenceProperty(str(binding.get("property")))
        except ValueError:
            return None
        if target is None:
            return None
        return VariableReference(service_id=target.id, property=prop)

    def suggest(
        self,
        key: str,
        *,
        host: str | None = None,
        scheme: str | None = None,
        engine: DatabaseEngine | None = None,
    ) -> VariableReference | None:
        """키·호스트·스킴이 가리키는 같은 프로젝트 서비스(같은 스택 우선)의 연결 정보."""
        prop = _property_for_key(key)
        if prop is None:
            return None
        wanted = engine or engine_hint(key) or _engine_for_scheme(scheme)
        candidates = sorted(
            self._others, key=lambda s: (s.stack_id != self._service.stack_id, s.id)
        )
        if host is not None:
            named = next(
                (s for s in candidates if host in (s.name, s.stack_unit_id)),
                None,
            )
            if named is not None and (
                named.kind == ServiceKind.DATABASE or prop != ReferenceProperty.DATABASE
            ):
                return VariableReference(service_id=named.id, property=prop)
        if wanted is None and key in _GENERIC_DATABASE_KEYS:
            databases = [s for s in candidates if s.kind == ServiceKind.DATABASE]
            relational = [
                s
                for s in databases
                if s.database_engine in (DatabaseEngine.POSTGRES, DatabaseEngine.MYSQL)
            ]
            if relational:
                return VariableReference(service_id=relational[0].id, property=prop)
            return None
        if wanted is None:
            return None
        match = next((s for s in candidates if s.database_engine == wanted), None)
        if match is None:
            return None
        return VariableReference(service_id=match.id, property=prop)


def _property_for_key(key: str) -> ReferenceProperty | None:
    if key.endswith(("_URL", "_URI")):
        return ReferenceProperty.URL
    if key.endswith("_HOST"):
        return ReferenceProperty.HOST
    if key.endswith("_PORT"):
        return ReferenceProperty.PORT
    if key.endswith(("_ADDR", "_ADDRESS")):
        return ReferenceProperty.HOST
    return None


def _engine_for_scheme(scheme: str | None) -> DatabaseEngine | None:
    if scheme is None:
        return None
    return next((s.engine for s in ENGINE_SPECS.values() if scheme in s.accepted_schemes), None)


def raise_invalid(validations: list[VariableValidation]) -> None:
    """error 가 있는 검증들로 422 VARIABLES_INVALID 를 만든다. 한 서비스면 data 가 검증 결과
    그대로다."""
    details = [FieldIssue(issue.key, issue.code.value) for v in validations for issue in v.errors]
    if len(validations) == 1:
        data: dict[str, Any] = validations[0].to_json()
        data["serviceId"] = validations[0].service_id
    else:
        data = {
            "ok": False,
            "issues": [issue.to_json() for v in validations for issue in v.issues],
            "services": [{"serviceId": v.service_id, **v.to_json()} for v in validations],
        }
    raise VariablesInvalidError(
        "environment variables are invalid",
        issues=details,
        data=data,
        service_ids=[v.service_id for v in validations],
    )
