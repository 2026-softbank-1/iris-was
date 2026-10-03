import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import VariableCipher
from app.core.exceptions import (
    FieldIssue,
    InvalidInputError,
    ServiceNotFoundError,
    VariableConflictError,
    VariableNotFoundError,
)
from app.enums import APP_PORT
from app.models.service import Service
from app.repositories.service_repository import ServiceRepository
from app.repositories.service_variable_repository import ServiceVariableRepository
from app.services.raw_variables import RawVariable, parse_raw_entries

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
    key: str
    value: str


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


def build_system_variables(service: Service) -> list[SystemVariable]:
    """앱에 자동으로 주입되는 변수. 값이 타깃·배포마다 다른 것은 값 없이 이름만 알린다."""
    return [
        SystemVariable("PORT", "앱이 listen 해야 하는 포트", str(APP_PORT)),
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
    """서비스 환경변수 CRUD. 값은 암호화해 저장하고, 평문은 응답을 만들 때만 복호화한다."""

    def __init__(
        self,
        session: AsyncSession,
        service_repository: ServiceRepository,
        service_variable_repository: ServiceVariableRepository,
        cipher: VariableCipher,
    ) -> None:
        self._session = session
        self._service_repository = service_repository
        self._service_variable_repository = service_variable_repository
        self._cipher = cipher

    async def search_variables(self, owner_id: int, service_id: int) -> ServiceVariables:
        """사용자 변수(키 순)와 플랫폼이 자동 주입하는 변수를 함께 돌려준다."""
        service = await self._get_owned(owner_id, service_id)
        variables = await self._service_variable_repository.search_by_service_id(service.id)
        return ServiceVariables(
            [VariableEntry(v.key, self._cipher.decrypt(v.encrypted_value)) for v in variables],
            build_system_variables(service),
        )

    async def create_variable(
        self, owner_id: int, service_id: int, key: str, value: str
    ) -> VariableEntry:
        """이미 있는 키면 `VariableConflictError`. 값을 바꾸려면 `update_variable` 을 쓴다."""
        service = await self._get_owned(owner_id, service_id)
        validate_variable(key, value)
        existing = await self._service_variable_repository.search_by_service_id(service.id)
        if len(existing) >= MAX_VARIABLES:
            raise InvalidInputError(
                "too many variables", field="key", service_id=service.id, limit=MAX_VARIABLES
            )
        created = await self._service_variable_repository.add_if_absent(
            service.id, key, self._cipher.encrypt(value)
        )
        if created is None:
            raise VariableConflictError("variable already exists", service_id=service.id, key=key)
        await self._session.commit()
        self._log_changed("create_variable", service.id, 1)
        return VariableEntry(key, value)

    async def update_variable(
        self, owner_id: int, service_id: int, key: str, value: str
    ) -> VariableEntry:
        service = await self._get_owned(owner_id, service_id)
        validate_variable(key, value)
        variable = await self._service_variable_repository.find_by_service_id_and_key(
            service.id, key
        )
        if variable is None:
            raise VariableNotFoundError("variable not found", service_id=service.id, key=key)
        variable.encrypted_value = self._cipher.encrypt(value)
        await self._session.commit()
        self._log_changed("update_variable", service.id, 1)
        return VariableEntry(key, value)

    async def delete_variable(self, owner_id: int, service_id: int, key: str) -> None:
        service = await self._get_owned(owner_id, service_id)
        variable = await self._service_variable_repository.find_by_service_id_and_key(
            service.id, key
        )
        if variable is None:
            raise VariableNotFoundError("variable not found", service_id=service.id, key=key)
        await self._service_variable_repository.delete(variable)
        await self._session.commit()
        self._log_changed("delete_variable", service.id, 1)

    async def replace_variables(self, owner_id: int, service_id: int, raw: str) -> ServiceVariables:
        """Raw 텍스트가 가리키는 집합으로 서비스의 변수를 통째로 바꾼다. 빠진 키는 지워진다."""
        service = await self._get_owned(owner_id, service_id)
        parsed = parse_raw_entries(raw)
        self._validate_all(service.id, parsed)
        await self._service_variable_repository.replace_all(
            service.id,
            {key: self._cipher.encrypt(entry.value) for key, entry in parsed.items()},
        )
        await self._session.commit()
        self._log_changed("replace_variables", service.id, len(parsed))
        return ServiceVariables(
            [VariableEntry(key, parsed[key].value) for key in sorted(parsed)],
            build_system_variables(service),
        )

    def _validate_all(self, service_id: int, variables: Mapping[str, RawVariable]) -> None:
        """하나라도 저장할 수 없으면 거부하고, 틀린 줄을 모두 details 로 알린다."""
        if len(variables) > MAX_VARIABLES:
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
