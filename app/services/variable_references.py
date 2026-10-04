"""참조 변수(`{serviceId, property}`)를 대상 서비스의 연결 정보로 푼다.

변수 API 는 비밀을 가린 미리보기로, Deploy Worker 는 봉인 직전에 실제 값으로 푼다. 대상은 같은
프로젝트의 지워지지 않은 다른 서비스여야 한다. 아니면 `VariableReferenceBrokenError` 다.
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

from app.core.crypto import VariableCipher
from app.core.exceptions import VariableReferenceBrokenError
from app.enums import ReferenceProperty, ServiceKind
from app.models.service import Service
from app.repositories.service_repository import ServiceRepository
from app.repositories.service_variable_repository import ServiceVariableRepository
from app.services.database_engines import (
    MASK,
    build_connection,
    get_engine_spec,
    url_template,
)
from app.services.service_networking import (
    app_connection,
    internal_host,
    internal_port,
    is_networking_available,
    supported_properties,
)

MAX_SUFFIX_LENGTH = 2048
MAX_USER_LENGTH = 128
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*$")
_SUFFIX_FORBIDDEN = re.compile(r"[\s\x00-\x1f\x7f]")
_USER = re.compile(r"^[A-Za-z0-9._~-]{1,128}$")
# 변수 키 규칙(variable_service.KEY_PATTERN)과 같다. 순환 import 를 피해 여기 둔다.
_VARIABLE_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def check_scheme(value: str | None) -> str | None:
    if value is not None and not _SCHEME.fullmatch(value):
        raise ValueError("scheme must match ^[a-z][a-z0-9+.-]*$")
    return value


def check_suffix(value: str | None) -> str | None:
    """경로·쿼리·프래그먼트(`/api/v1?tenant=demo`). 빈 값은 없는 것과 같아 None 으로 정리한다."""
    if not value:
        return None
    if len(value) > MAX_SUFFIX_LENGTH:
        raise ValueError(f"suffix must be at most {MAX_SUFFIX_LENGTH} characters")
    if value[0] not in "/?#":
        raise ValueError("suffix must start with /, ? or #")
    if _SUFFIX_FORBIDDEN.search(value):
        raise ValueError("suffix must not contain whitespace or control characters")
    return value


def check_user(value: str | None) -> str | None:
    """URL 에 넣을 DB 사용자. 영문·숫자·`._~-` 만, 128자 이하(URL 인코딩이 필요 없는 문자)."""
    if value is not None and not _USER.fullmatch(value):
        raise ValueError("user must match ^[A-Za-z0-9._~-]{1,128}$")
    return value


def check_password_variable(value: str | None) -> str | None:
    if value is not None and not _VARIABLE_KEY.fullmatch(value):
        raise ValueError("passwordVariable must be a variable key")
    return value


ReferenceScheme = Annotated[str | None, AfterValidator(check_scheme)]
ReferenceSuffix = Annotated[str | None, AfterValidator(check_suffix)]
ReferenceUser = Annotated[str | None, AfterValidator(check_user)]
ReferencePasswordVariable = Annotated[str | None, AfterValidator(check_password_variable)]


class VariableReference(BaseModel):
    """저장 형태이자 API 형태(camelCase). 값은 담지 않는다.

    `scheme`·`suffix`·`user`·`passwordVariable` 은 property=url 일 때만 있다. 앞의 둘은 코드가 쓴
    URL 의 스킴과 경로·쿼리·프래그먼트라 풀 때 호스트·포트 뒤에 그대로 붙는다(없으면 기본 스킴,
    접미사 없음). `user` 가 있으면 DB 관리 사용자 대신 그 사용자로 userinfo 를 만들고, 비밀번호는
    같은 서비스의 변수 `passwordVariable` 값(없으면 DB 관리 비밀번호)이다. DB 대상에만 쓴다.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    service_id: int = Field(gt=0)
    property: ReferenceProperty
    scheme: ReferenceScheme = None
    suffix: ReferenceSuffix = None
    user: ReferenceUser = None
    password_variable: ReferencePasswordVariable = None

    @model_validator(mode="after")
    def _url_only(self) -> "VariableReference":
        if self.property != ReferenceProperty.URL and (
            self.scheme or self.suffix or self.user or self.password_variable
        ):
            raise ValueError(
                "scheme, suffix, user and passwordVariable are only for the url property"
            )
        if self.password_variable and not self.user:
            raise ValueError("passwordVariable needs user")
        return self

    def has_credentials(self) -> bool:
        """DB 관리 자격 증명 대신 코드가 쓴 사용자(와 비밀값)로 URL 을 만든다."""
        return self.user is not None

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> "VariableReference":
        return cls.model_validate(value)


def _safe(check: Callable[[str | None], str | None], value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return check(value)
    except ValueError:
        return None


def reference_from_parts(
    service_id: int,
    prop: ReferenceProperty,
    scheme: object,
    suffix: object,
    *,
    user: object = None,
    password_variable: object = None,
) -> VariableReference:
    """분석기 binding 의 scheme·urlSuffix·user(신뢰하지 않는 입력)로 참조를 만든다. 틀린 값은
    버린다. 사용자가 없으면 passwordVariable 도 버린다(DB 관리 자격 증명을 쓴다)."""
    if prop != ReferenceProperty.URL:
        return VariableReference(service_id=service_id, property=prop)
    safe_user = _safe(check_user, user)
    return VariableReference(
        service_id=service_id,
        property=prop,
        scheme=_safe(check_scheme, scheme),
        suffix=_safe(check_suffix, suffix),
        user=safe_user,
        password_variable=(
            _safe(check_password_variable, password_variable) if safe_user else None
        ),
    )


@dataclass(frozen=True)
class ResolvedReference:
    target: Service
    value: str


class ReferenceResolver:
    """`cipher` 가 없으면 비밀 속성(password·DB url)은 미리보기(가린 값)만 만들 수 있다."""

    def __init__(
        self,
        service_repository: ServiceRepository,
        service_variable_repository: ServiceVariableRepository,
        *,
        is_networking_enabled: bool,
        cipher: VariableCipher | None = None,
    ) -> None:
        self._service_repository = service_repository
        self._service_variable_repository = service_variable_repository
        self._is_networking_enabled = is_networking_enabled
        self._cipher = cipher

    async def get_target(self, owner: Service, reference: VariableReference) -> Service:
        """같은 프로젝트의 다른 서비스이고 그 속성이 있으면 대상 서비스를 돌려준다."""
        target = await self._service_repository.find_active_by_id(reference.service_id)
        if target is None or target.project_id != owner.project_id or target.id == owner.id:
            raise VariableReferenceBrokenError(
                "referenced service is not in this project",
                service_id=owner.id,
                target_service_id=reference.service_id,
            )
        if reference.property not in supported_properties(target):
            raise VariableReferenceBrokenError(
                "referenced service has no such property",
                service_id=owner.id,
                target_service_id=target.id,
                property=reference.property,
            )
        if reference.has_credentials() and target.kind != ServiceKind.DATABASE:
            raise VariableReferenceBrokenError(
                "user and passwordVariable are only for database references",
                service_id=owner.id,
                target_service_id=target.id,
            )
        return target

    async def resolve(
        self,
        owner: Service,
        reference: VariableReference,
        *,
        masked: bool,
        owner_values: Mapping[str, str] | None = None,
    ) -> ResolvedReference:
        """`owner_values` 는 owner 의 값 변수 평문(배포 스냅샷)이다. `passwordVariable` 을 여기서
        먼저 찾고, 없으면 owner 의 지금 변수에서 읽는다."""
        target = await self.get_target(owner, reference)
        networking = is_networking_available(
            self._is_networking_enabled, await self._service_repository.find_target_kind(target.id)
        )
        if target.kind != ServiceKind.DATABASE or target.database_engine is None:
            value = app_connection(target, is_networking=networking).property(
                reference.property, scheme=reference.scheme, suffix=reference.suffix
            )
            assert value is not None
            return ResolvedReference(target, value)
        engine = target.database_engine
        host = internal_host(target.id)
        port = internal_port(target, is_networking=networking)
        if masked:
            return ResolvedReference(target, self._preview(target, reference, host, port))
        if self._cipher is None:
            raise VariableReferenceBrokenError(
                "database credentials cannot be decrypted", target_service_id=target.id
            )
        spec = get_engine_spec(engine)
        variables = {
            v.key: self._cipher.decrypt(v.encrypted_value)
            for v in await self._service_variable_repository.search_by_service_id(target.id)
            if v.key in spec.managed_keys and v.encrypted_value is not None
        }
        connection = build_connection(engine, host, port, variables)
        if reference.user is not None:
            password = connection.password
            if reference.password_variable is not None:
                password = await self._owner_secret(
                    owner, reference.password_variable, owner_values
                )
            connection = connection.with_user(reference.user, password)
        value = connection.property(
            reference.property, scheme=reference.scheme, suffix=reference.suffix
        )
        if value is None:
            raise VariableReferenceBrokenError(
                "referenced service has no such property",
                target_service_id=target.id,
                property=reference.property,
            )
        return ResolvedReference(target, value)

    async def _owner_secret(
        self, owner: Service, key: str, owner_values: Mapping[str, str] | None
    ) -> str:
        """owner 의 값 변수(비밀값) 평문. 없거나 참조 변수면 끊어진 참조다."""
        if owner_values is not None and key in owner_values:
            return owner_values[key]
        assert self._cipher is not None
        variable = await self._service_variable_repository.find_by_service_id_and_key(owner.id, key)
        if variable is None or variable.encrypted_value is None:
            raise VariableReferenceBrokenError(
                "passwordVariable is not a value variable of this service",
                service_id=owner.id,
                key=key,
            )
        return self._cipher.decrypt(variable.encrypted_value)

    @staticmethod
    def _preview(target: Service, reference: VariableReference, host: str, port: int) -> str:
        """비밀을 가린 값. DB 사용자·데이터베이스 이름은 설정에 남긴 값을 쓴다."""
        assert target.database_engine is not None
        config = target.database_config or {}
        match reference.property:
            case ReferenceProperty.URL:
                return url_template(
                    target.database_engine,
                    host,
                    port,
                    config,
                    scheme=reference.scheme,
                    suffix=reference.suffix,
                    user=reference.user,
                )
            case ReferenceProperty.HOST:
                return host
            case ReferenceProperty.PORT:
                return str(port)
            case ReferenceProperty.PASSWORD:
                return MASK
            case ReferenceProperty.USER:
                return str(config.get("user") or "default")
            case ReferenceProperty.DATABASE:
                return str(config.get("database") or "")
