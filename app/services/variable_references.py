"""참조 변수(`{serviceId, property}`)를 대상 서비스의 연결 정보로 푼다.

변수 API 는 비밀을 가린 미리보기로, Deploy Worker 는 봉인 직전에 실제 값으로 푼다. 대상은 같은
프로젝트의 지워지지 않은 다른 서비스여야 한다. 아니면 `VariableReferenceBrokenError` 다.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
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


class VariableReference(BaseModel):
    """저장 형태이자 API 형태(camelCase). 값은 담지 않는다."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    service_id: int = Field(gt=0)
    property: ReferenceProperty

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> "VariableReference":
        return cls.model_validate(value)


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
        return target

    async def resolve(
        self, owner: Service, reference: VariableReference, *, masked: bool
    ) -> ResolvedReference:
        target = await self.get_target(owner, reference)
        networking = is_networking_available(
            self._is_networking_enabled, await self._service_repository.find_target_kind(target.id)
        )
        if target.kind != ServiceKind.DATABASE or target.database_engine is None:
            value = app_connection(target, is_networking=networking).property(reference.property)
            assert value is not None
            return ResolvedReference(target, value)
        engine = target.database_engine
        host = internal_host(target.id)
        port = internal_port(target, is_networking=networking)
        if masked:
            return ResolvedReference(target, self._preview(target, reference.property, host, port))
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
        value = build_connection(engine, host, port, variables).property(reference.property)
        if value is None:
            raise VariableReferenceBrokenError(
                "referenced service has no such property",
                target_service_id=target.id,
                property=reference.property,
            )
        return ResolvedReference(target, value)

    @staticmethod
    def _preview(target: Service, name: ReferenceProperty, host: str, port: int) -> str:
        """비밀을 가린 값. DB 사용자·데이터베이스 이름은 설정에 남긴 값을 쓴다."""
        assert target.database_engine is not None
        config = target.database_config or {}
        match name:
            case ReferenceProperty.URL:
                return url_template(target.database_engine, host, port, config)
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
