from dataclasses import dataclass

from app.core.exceptions import ServiceNotFoundError
from app.models.service import Service
from app.models.target import Target
from app.repositories.release_repository import ReleaseRepository
from app.repositories.service_repository import ServiceRepository
from app.repositories.target_repository import TargetRepository
from app.services.service_registry_service import slugify_service_name

DNS_LABEL_MAX_LENGTH = 63


def service_host_label(name: str, service_id: int, server_key: str | None = None) -> str:
    """서비스 도메인의 첫 label. chart 는 소문자·숫자·하이픈 DNS label(63자 이하)만 받는다.

    이름은 사용자가 정할 수 있고 프로젝트 안에서만 유일해서, 변환한 뒤 service_id 를 붙인다.
    사용자가 등록한 서버의 타깃이면 끝에 `-{serverKey}` 를 더 붙인다. 게이트웨이는 마지막 `-` 뒤의
    영문으로 시작하는 8자로 서버를 고른다. 63자를 넘으면 서비스 이름 부분을 줄인다.
    """
    suffix = f"-{service_id}" if server_key is None else f"-{service_id}-{server_key}"
    return slugify_service_name(name, DNS_LABEL_MAX_LENGTH - len(suffix)) + suffix


def target_server_key(target: Target) -> str | None:
    """사용자가 등록한 서버의 타깃이면 그 serverKey. 공용 타깃이면 None 이다.

    `target.onprem_server` 를 함께 읽은 타깃이어야 한다.
    """
    return target.onprem_server.server_key if target.onprem_server is not None else None


def build_service_host(
    name: str, service_id: int, domain_suffix: str, server_key: str | None = None
) -> str:
    """`{서비스 이름}-{service_id}.{타깃 도메인 접미사}`. 접미사는 `likelion.uk` 처럼 점 하나다.

    등록한 서버의 타깃이면 `{서비스 이름}-{service_id}-{serverKey}.internal.likelion.uk` 다.
    """
    return f"{service_host_label(name, service_id, server_key)}.{domain_suffix}"


@dataclass(frozen=True)
class ServiceDomainDetail:
    target: Target
    # 타깃에 도메인 접미사가 없으면 None 이다.
    host: str | None
    # 이 타깃에 SUCCEEDED release 가 있다. 그 전에는 주소가 있어도 앱이 응답하지 않는다(503).
    is_connected: bool


class DomainService:
    """서비스 도메인 조회. 주소는 저장하지 않고 이름·번호·타깃 접미사로 계산한다."""

    def __init__(
        self,
        service_repository: ServiceRepository,
        target_repository: TargetRepository,
        release_repository: ReleaseRepository,
    ) -> None:
        self._service_repository = service_repository
        self._target_repository = target_repository
        self._release_repository = release_repository

    async def search_domains(self, owner_id: int, service_id: int) -> list[ServiceDomainDetail]:
        """서비스가 연결한 타깃마다 한 건. 도메인 규칙이 없는 타깃은 `host` 가 None 이다."""
        service = await self._get_owned(owner_id, service_id)
        target_ids = await self._service_repository.search_target_ids_by_service_ids([service.id])
        targets = await self._target_repository.search_by_ids(target_ids[service.id])
        return [await self._detail(service, target) for target in targets]

    async def _get_owned(self, owner_id: int, service_id: int) -> Service:
        service = await self._service_repository.find_by_id_and_owner_id(service_id, owner_id)
        if service is None:
            raise ServiceNotFoundError("service not found", service_id=service_id)
        return service

    async def _detail(self, service: Service, target: Target) -> ServiceDomainDetail:
        if target.domain_suffix is None:
            return ServiceDomainDetail(target, None, False)
        host = build_service_host(
            service.name, service.id, target.domain_suffix, target_server_key(target)
        )
        release = await self._release_repository.find_last_known_good(service.id, target.id)
        return ServiceDomainDetail(target, host, release is not None)
