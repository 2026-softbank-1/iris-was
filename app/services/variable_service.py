import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import VariableCipher
from app.core.exceptions import (
    FieldIssue,
    InvalidInputError,
    ServiceNotFoundError,
    VariableConflictError,
    VariableNotFoundError,
    VariableReferenceBrokenError,
)
from app.enums import APP_PORT, ServiceKind
from app.models.service import Service
from app.models.service_variable import ServiceVariable
from app.repositories.service_repository import ServiceRepository
from app.repositories.service_variable_repository import ServiceVariableRepository
from app.services.database_engines import get_engine_spec
from app.services.raw_variables import RawVariable, parse_raw_entries
from app.services.service_networking import (
    container_port,
    is_networking_available,
    networking_unavailable_reason,
)
from app.services.variable_references import ReferenceResolver, VariableReference

logger = logging.getLogger(__name__)

KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_KEY_LENGTH = 128
MAX_VALUE_LENGTH = 32 * 1024
MAX_VARIABLES = 100
# 플랫폼이 주입하는 이름. chart 의 env 가 사용자 변수보다 우선해 덮어쓸 수 없으므로 저장도 막는다.
RESERVED_KEYS = frozenset({"PORT"})
RESERVED_PREFIX = "IRIS_"
# Raw 저장이 거부될 때 응답 details 에 싣는 줄 수. 큰 파일에서 응답이 커지지 않게 막는다.
MAX_RAW_ISSUES = 20


@dataclass(frozen=True)
class VariableEntry:
    """값 변수는 value, 참조 변수는 reference 와 resolved(비밀을 가린 미리보기)가 있다."""

    key: str
    value: str | None
    reference: VariableReference | None = None
    resolved: str | None = None


@dataclass(frozen=True)
class SystemVariable:
    """플랫폼이 배포할 때 앱에 주입하는 변수. 서비스만으로 값이 정해지지 않으면 `value` 가 None."""

    key: str
    description: str
    value: str | None = None


@dataclass(frozen=True)
class ServiceVariables:
    variables: list[VariableEntry]
    system_variables: list[SystemVariable]


def build_system_variables(service: Service, port: int = APP_PORT) -> list[SystemVariable]:
    """앱에 자동으로 주입되는 변수. 값이 타깃·배포마다 다른 것은 값 없이 이름만 알린다."""
    return [
        SystemVariable("PORT", "앱이 listen 해야 하는 포트", str(port)),
        SystemVariable("IRIS_SERVICE_NAME", "서비스 이름", service.name),
        SystemVariable("IRIS_TARGET_NAME", "배포되는 타깃 이름. 배포할 때 정해진다"),
        SystemVariable("IRIS_DEPLOYMENT_ID", "이 앱을 띄운 배포 요청 id. 배포할 때 정해진다"),
        SystemVariable("IRIS_PUBLIC_DOMAIN", "서비스의 공개 도메인. 타깃마다 다르다"),
        SystemVariable("IRIS_GIT_COMMIT_SHA", "배포한 소스 커밋 SHA. 배포할 때 정해진다"),
    ]


@dataclass(frozen=True)
class VariableProblem:
    """저장할 수 없는 변수 하나. message 는 응답 message(계약), reason 은 사람이 읽는 사유다."""

    message: str
    reason: str
    field: str
    key: str


def managed_variable_keys(service: Service) -> frozenset[str]:
    """플랫폼이 만들고 관리하는 변수(관리형 DB 의 자격 증명). 변수 API 로 바꿀 수 없다."""
    if service.kind != ServiceKind.DATABASE or service.database_engine is None:
        return frozenset()
    return get_engine_spec(service.database_engine).managed_keys


def build_database_system_variables(
    service: Service, plaintexts: Mapping[str, str]
) -> list[SystemVariable]:
    """관리형 DB 가 쓰는 자격 증명 변수. 비밀번호는 값을 내지 않는다."""
    assert service.database_engine is not None
    spec = get_engine_spec(service.database_engine)
    variables: list[SystemVariable] = []
    if spec.user_key:
        variables.append(SystemVariable(spec.user_key, "DB 사용자", plaintexts.get(spec.user_key)))
    if spec.database_key:
        variables.append(
            SystemVariable(spec.database_key, "DB 이름", plaintexts.get(spec.database_key))
        )
    for key in sorted(spec.secret_keys):
        variables.append(
            SystemVariable(key, "플랫폼이 만든 비밀번호. 값은 보여 주지 않고 참조 변수로 쓴다")
        )
    return variables


def check_variable(key: str, value: str) -> VariableProblem | None:
    """키·값이 저장할 수 있는 형태인지 본다. 값은 비어 있어도 된다."""
    shown = key[:MAX_KEY_LENGTH]
    if not KEY_PATTERN.match(key) or len(key) > MAX_KEY_LENGTH:
        reason = (
            f"key {key[:32]}... is longer than {MAX_KEY_LENGTH} characters"
            if len(key) > MAX_KEY_LENGTH
            else f"key {key} must be letters, digits and underscores, not starting with a digit"
        )
        return VariableProblem(
            "variable key must be letters, digits and underscores, not starting with a digit",
            reason,
            "key",
            shown,
        )
    if key in RESERVED_KEYS or key.startswith(RESERVED_PREFIX):
        return VariableProblem(
            "variable key is reserved by the platform", f"reserved key {key}", "key", key
        )
    if len(value) > MAX_VALUE_LENGTH:
        return VariableProblem(
            "variable value is too long",
            f"value of {key} is longer than {MAX_VALUE_LENGTH} characters",
            "value",
            key,
        )
    return None


def validate_variable(key: str, value: str) -> None:
    problem = check_variable(key, value)
    if problem is not None:
        raise InvalidInputError(problem.message, field=problem.field, key=problem.key)


class VariableService:
    """서비스 환경변수 CRUD. 값은 암호화해 저장하고, 평문은 응답을 만들 때만 복호화한다.

    값 대신 같은 프로젝트 다른 서비스의 연결 정보를 가리키는 참조 변수도 만든다. 관리형 DB 의
    자격 증명은 플랫폼이 관리해 목록(`variables`)에 내지 않고 바꿀 수도 없다.
    """

    def __init__(
        self,
        session: AsyncSession,
        service_repository: ServiceRepository,
        service_variable_repository: ServiceVariableRepository,
        cipher: VariableCipher,
        *,
        is_networking_enabled: bool = False,
    ) -> None:
        self._session = session
        self._service_repository = service_repository
        self._service_variable_repository = service_variable_repository
        self._cipher = cipher
        self._is_networking_enabled = is_networking_enabled
        self._resolver = ReferenceResolver(
            service_repository,
            service_variable_repository,
            is_networking_enabled=is_networking_enabled,
            cipher=cipher,
        )

    async def search_variables(self, owner_id: int, service_id: int) -> ServiceVariables:
        """사용자 변수(키 순)와 플랫폼이 자동 주입하는 변수를 함께 돌려준다."""
        service = await self._get_owned(owner_id, service_id)
        variables = await self._service_variable_repository.search_by_service_id(service.id)
        managed = managed_variable_keys(service)
        entries = [
            await self._to_entry(service, variable)
            for variable in variables
            if variable.key not in managed
        ]
        if service.kind == ServiceKind.DATABASE:
            plaintexts = {
                v.key: self._cipher.decrypt(v.encrypted_value)
                for v in variables
                if v.key in managed
                and v.encrypted_value is not None
                and v.key not in get_engine_spec(service.database_engine or "postgres").secret_keys
            }
            return ServiceVariables(entries, build_database_system_variables(service, plaintexts))
        return ServiceVariables(entries, build_system_variables(service, await self._port(service)))

    async def create_variable(
        self,
        owner_id: int,
        service_id: int,
        key: str,
        value: str | None,
        reference: VariableReference | None = None,
    ) -> VariableEntry:
        """이미 있는 키면 `VariableConflictError`. 값을 바꾸려면 `update_variable` 을 쓴다.

        값과 참조 중 하나만 준다.
        """
        service = await self._get_owned(owner_id, service_id)
        self._check_writable(service, key)
        validate_variable(key, value or "")
        stored_reference = await self._check_reference(service, reference)
        existing = await self._service_variable_repository.search_by_service_id(service.id)
        if len(existing) >= MAX_VARIABLES:
            raise InvalidInputError(
                "too many variables", field="key", service_id=service.id, limit=MAX_VARIABLES
            )
        created = await self._service_variable_repository.add_if_absent(
            service.id,
            key,
            self._cipher.encrypt(value) if reference is None and value is not None else None,
            stored_reference,
        )
        if created is None:
            raise VariableConflictError("variable already exists", service_id=service.id, key=key)
        await self._session.commit()
        self._log_changed("create_variable", service.id, 1)
        return await self._to_entry(service, created)

    async def update_variable(
        self,
        owner_id: int,
        service_id: int,
        key: str,
        value: str | None,
        reference: VariableReference | None = None,
    ) -> VariableEntry:
        """값 변수를 참조로, 참조를 값으로 바꿀 수도 있다."""
        service = await self._get_owned(owner_id, service_id)
        self._check_writable(service, key)
        validate_variable(key, value or "")
        stored_reference = await self._check_reference(service, reference)
        variable = await self._service_variable_repository.find_by_service_id_and_key(
            service.id, key
        )
        if variable is None:
            raise VariableNotFoundError("variable not found", service_id=service.id, key=key)
        if stored_reference is not None:
            variable.encrypted_value = None
            variable.reference = stored_reference
        else:
            variable.reference = None
            variable.encrypted_value = self._cipher.encrypt(value or "")
        await self._session.commit()
        self._log_changed("update_variable", service.id, 1)
        return await self._to_entry(service, variable)

    async def delete_variable(self, owner_id: int, service_id: int, key: str) -> None:
        service = await self._get_owned(owner_id, service_id)
        self._check_writable(service, key)
        variable = await self._service_variable_repository.find_by_service_id_and_key(
            service.id, key
        )
        if variable is None:
            raise VariableNotFoundError("variable not found", service_id=service.id, key=key)
        await self._service_variable_repository.delete(variable)
        await self._session.commit()
        self._log_changed("delete_variable", service.id, 1)

    async def replace_variables(self, owner_id: int, service_id: int, raw: str) -> ServiceVariables:
        """Raw 텍스트가 가리키는 집합으로 서비스의 값 변수를 통째로 바꾼다. 빠진 키는 지워진다.

        Raw 텍스트로 쓸 수 없는 참조 변수는 텍스트에 그 키가 없으면 그대로 둔다(있으면 값 변수가
        된다). 관리형 DB 의 자격 증명은 텍스트와 상관없이 그대로다.
        """
        service = await self._get_owned(owner_id, service_id)
        parsed = parse_raw_entries(raw)
        managed = managed_variable_keys(service)
        for key in parsed:
            self._check_writable(service, key)
        current = await self._service_variable_repository.search_by_service_id(service.id)
        keep_keys = {
            v.key for v in current if v.key in managed or (v.is_reference and v.key not in parsed)
        }
        self._validate_all(service.id, parsed, extra_count=len(keep_keys - managed))
        await self._service_variable_repository.replace_all(
            service.id,
            {key: self._cipher.encrypt(entry.value) for key, entry in parsed.items()},
            keep_keys,
        )
        await self._session.commit()
        self._log_changed("replace_variables", service.id, len(parsed))
        return await self.search_variables(owner_id, service_id)

    async def _to_entry(self, service: Service, variable: ServiceVariable) -> VariableEntry:
        if variable.reference is None:
            assert variable.encrypted_value is not None
            return VariableEntry(variable.key, self._cipher.decrypt(variable.encrypted_value))
        reference = VariableReference.from_json(variable.reference)
        try:
            resolved: str | None = (
                await self._resolver.resolve(service, reference, masked=True)
            ).value
        except VariableReferenceBrokenError:
            resolved = None
        return VariableEntry(variable.key, None, reference, resolved)

    async def _check_reference(
        self, service: Service, reference: VariableReference | None
    ) -> dict[str, Any] | None:
        """같은 프로젝트의 다른 서비스와 그 속성인지 본다. 서비스 사이 통신이 되는 타깃이어야
        한다."""
        if reference is None:
            return None
        target_kind = await self._service_repository.find_target_kind(service.id)
        if not is_networking_available(self._is_networking_enabled, target_kind):
            raise InvalidInputError(
                "variable references need project networking",
                issues=[
                    FieldIssue(
                        "reference", networking_unavailable_reason(self._is_networking_enabled)
                    )
                ],
                field="reference",
                service_id=service.id,
            )
        await self._resolver.get_target(service, reference)
        return reference.to_json()

    @staticmethod
    def _check_writable(service: Service, key: str) -> None:
        if key in managed_variable_keys(service):
            raise InvalidInputError(
                "variable is managed by the platform",
                issues=[FieldIssue("key", "managed_by_platform")],
                field="key",
                key=key,
                service_id=service.id,
            )

    async def _port(self, service: Service) -> int:
        target_kind = await self._service_repository.find_target_kind(service.id)
        return container_port(
            service,
            is_networking=is_networking_available(self._is_networking_enabled, target_kind),
        )

    def _validate_all(
        self, service_id: int, variables: Mapping[str, RawVariable], extra_count: int = 0
    ) -> None:
        """하나라도 저장할 수 없으면 거부하고, 틀린 줄을 모두 details 로 알린다."""
        if len(variables) + extra_count > MAX_VARIABLES:
            raise InvalidInputError(
                "too many variables",
                issues=[
                    FieldIssue(
                        "raw", f"{len(variables)} variables, at most {MAX_VARIABLES} allowed"
                    )
                ],
                field="raw",
                service_id=service_id,
                limit=MAX_VARIABLES,
            )
        problems = [
            (variable.line, problem)
            for key, variable in variables.items()
            if (problem := check_variable(key, variable.value)) is not None
        ]
        if not problems:
            return
        problems.sort(key=lambda found: found[0])
        first = problems[0][1]
        raise InvalidInputError(
            first.message,
            issues=[
                FieldIssue("raw", f"line {line}: {problem.reason}")
                for line, problem in problems[:MAX_RAW_ISSUES]
            ],
            field=first.field,
            key=first.key,
            problem_count=len(problems),
        )

    async def _get_owned(self, owner_id: int, service_id: int) -> Service:
        service = await self._service_repository.find_by_id_and_owner_id(service_id, owner_id)
        if service is None:
            raise ServiceNotFoundError("service not found", service_id=service_id)
        return service

    @staticmethod
    def _log_changed(action: str, service_id: int, count: int) -> None:
        # 키·값은 남기지 않는다. 변경 건수만 기록한다.
        logger.info(
            "service variables changed",
            extra={"action": action, "service_id": service_id, "variable_count": count},
        )
