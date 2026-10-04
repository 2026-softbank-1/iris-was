"""프로젝트 안 서비스끼리 부르는 주소 규칙. API 응답·변수 검증·Deploy Worker 가 같은 규칙을 쓴다.

서비스마다 namespace `svc-{id}` 에 ClusterIP Service `app` 이 있다. 앱은 port 80(→ containerPort),
chart 0.8.0 의 `service.exposeContainerPort` 를 켜면 containerPort 로도 열린다. DB 는 엔진 포트다.
호스트 별칭은 서비스 namespace 의 ExternalName Service 라 별칭 이름만으로(`api:3000`) 풀린다.
"""

import re
from dataclasses import dataclass

from app.core.exceptions import FieldIssue, InvalidInputError
from app.enums import APP_PORT, ReferenceProperty, ServiceKind, TargetKind
from app.models.service import Service
from app.services.database_engines import get_engine_spec

# iris-service chart 의 `service.port`. 서비스 기본 포트다.
SERVICE_PORT = 80
# chart 가 만드는 Service 이름. 별칭으로 쓸 수 없다.
_RESERVED_ALIAS_NAMES = frozenset({"app"})
_ALIAS_NAME = re.compile(r"^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$")
MAX_HOST_ALIASES = 20
APP_PROPERTIES = (ReferenceProperty.URL, ReferenceProperty.HOST, ReferenceProperty.PORT)
# 관리형 DB·별칭·프로젝트 통신(chart 0.8.0)을 쓸 수 있는 타깃. on-prem 은 이전 chart 에 남는다.
NETWORKING_TARGET_KINDS = frozenset({TargetKind.AWS})


def networking_unavailable_reason(is_enabled: bool) -> str:
    """기능을 쓸 수 없는 이유(응답 details.reason)."""
    return "project_networking_disabled" if not is_enabled else "networking_unsupported_target"


def internal_host(service_id: int) -> str:
    return f"app.svc-{service_id}.svc.cluster.local"


def is_networking_available(is_enabled: bool, target_kind: TargetKind | None) -> bool:
    return is_enabled and target_kind in NETWORKING_TARGET_KINDS


def container_port(service: Service, *, is_networking: bool) -> int:
    """앱이 listen 하는 포트(`PORT`). 스택 앱은 분석한 포트를 써 compose 와 같은 `api:3000` 이 된다.

    그 밖의 서비스와 기능이 꺼진 경우는 이전과 같은 APP_PORT 다(values 가 바뀌지 않게).
    """
    if is_networking and service.stack_id is not None and service.port is not None:
        return service.port
    return APP_PORT


def internal_port(service: Service, *, is_networking: bool) -> int:
    """같은 프로젝트 다른 서비스가 `internal_host` 와 함께 쓰는 포트."""
    if service.kind == ServiceKind.DATABASE and service.database_engine is not None:
        return get_engine_spec(service.database_engine).port
    if is_networking:
        return container_port(service, is_networking=True)
    return SERVICE_PORT


@dataclass(frozen=True)
class AppConnection:
    host: str
    port: int

    def property(self, name: ReferenceProperty) -> str | None:
        match name:
            case ReferenceProperty.URL:
                return f"http://{self.host}:{self.port}"
            case ReferenceProperty.HOST:
                return self.host
            case ReferenceProperty.PORT:
                return str(self.port)
            case _:
                return None


def app_connection(service: Service, *, is_networking: bool) -> AppConnection:
    return AppConnection(
        internal_host(service.id), internal_port(service, is_networking=is_networking)
    )


def supported_properties(service: Service) -> list[ReferenceProperty]:
    if service.kind == ServiceKind.DATABASE and service.database_engine is not None:
        return get_engine_spec(service.database_engine).properties
    return list(APP_PROPERTIES)


def is_valid_alias_name(name: str) -> bool:
    return bool(_ALIAS_NAME.fullmatch(name)) and name not in _RESERVED_ALIAS_NAMES


def validate_host_aliases(
    service: Service, aliases: list[dict[str, object]], project_services: dict[int, Service]
) -> list[dict[str, object]]:
    """별칭을 정리해 돌려준다. 이름은 DNS 레이블, 대상은 같은 프로젝트의 다른 서비스여야 한다."""
    if len(aliases) > MAX_HOST_ALIASES:
        raise InvalidInputError(
            "too many host aliases",
            issues=[FieldIssue("hostAliases", f"at most {MAX_HOST_ALIASES} aliases")],
            field="hostAliases",
        )
    names: set[str] = set()
    cleaned: list[dict[str, object]] = []
    for alias in aliases:
        name = str(alias.get("name") or "")
        # API 본문(snake_case)과 저장 형태(camelCase)를 모두 받는다.
        target_id = alias.get("targetServiceId", alias.get("target_service_id"))
        if not is_valid_alias_name(name):
            raise InvalidInputError(
                "host alias name must be a DNS label other than app",
                issues=[FieldIssue("hostAliases", "invalid_alias_name")],
                field="hostAliases",
            )
        if name in names:
            raise InvalidInputError(
                "host alias names must not repeat",
                issues=[FieldIssue("hostAliases", "duplicate_alias_name")],
                field="hostAliases",
            )
        if (
            not isinstance(target_id, int)
            or target_id == service.id
            or (target_id not in project_services)
        ):
            raise InvalidInputError(
                "host alias target must be another service in the same project",
                issues=[FieldIssue("hostAliases", "invalid_alias_target")],
                field="hostAliases",
            )
        names.add(name)
        entry: dict[str, object] = {"name": name, "targetServiceId": target_id}
        port = alias.get("port")
        if isinstance(port, int):
            entry["port"] = port
        cleaned.append(entry)
    return cleaned
