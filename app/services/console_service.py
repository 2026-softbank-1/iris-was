"""서비스 콘솔(Pod 셸) — 열 수 있는지 판정하고 Console Gateway 용 ticket 을 발급한다(ADR 0033).

Control API 는 클러스터를 부르지 않는다. 사용자 인증·소유권·떠 있는 release 를 확인하고, 60초짜리
ticket 을 서명하고, 발급 기록(`console_sessions`)을 남길 뿐이다.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.console_ticket import CONSOLE_CLUSTER_AWS, ConsoleTicketSigner
from app.core.exceptions import (
    ConsoleTargetNotSupportedError,
    NoRunningDeploymentError,
    NotConfiguredError,
    NotFoundError,
    ServiceNotFoundError,
)
from app.enums import ConsoleUnavailableReason, TargetKind
from app.models.console_session import ConsoleSession
from app.models.release import Release
from app.models.service import Service
from app.models.target import Target
from app.repositories.console_session_repository import ConsoleSessionRepository
from app.repositories.release_repository import ReleaseRepository
from app.repositories.service_repository import ServiceRepository
from app.repositories.target_repository import TargetRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConsoleGatewayAddress:
    """화면이 Console Gateway 에 붙는 주소(끝의 `/` 를 뺀 base)."""

    http_url: str
    ws_url: str


@dataclass(frozen=True)
class ConsoleAvailability:
    is_available: bool
    reason: ConsoleUnavailableReason | None


@dataclass(frozen=True)
class IssuedConsoleSession:
    session_id: str
    token: str
    expires_at: datetime
    gateway: ConsoleGatewayAddress


class ConsoleService:
    def __init__(
        self,
        session: AsyncSession,
        service_repository: ServiceRepository,
        target_repository: TargetRepository,
        release_repository: ReleaseRepository,
        console_session_repository: ConsoleSessionRepository,
        private_key: str | None,
        gateway: ConsoleGatewayAddress | None,
    ) -> None:
        self._session = session
        self._service_repository = service_repository
        self._target_repository = target_repository
        self._release_repository = release_repository
        self._console_session_repository = console_session_repository
        self._private_key = private_key
        self._gateway = gateway

    async def get_availability(
        self, owner_id: int, service_id: int, target_id: int
    ) -> ConsoleAvailability:
        service, target = await self._get_scope(owner_id, service_id, target_id)
        reason, _ = await self._check(service, target)
        return ConsoleAvailability(is_available=reason is None, reason=reason)

    async def create_session(
        self, owner_id: int, service_id: int, target_id: int
    ) -> IssuedConsoleSession:
        service, target = await self._get_scope(owner_id, service_id, target_id)
        reason, release = await self._check(service, target)
        if reason == ConsoleUnavailableReason.TARGET_NOT_SUPPORTED:
            raise ConsoleTargetNotSupportedError(
                "console is not supported for this target", target_id=target.id
            )
        if reason == ConsoleUnavailableReason.NOT_CONFIGURED:
            raise NotConfiguredError(
                "console is not configured",
                setting="CONSOLE_TICKET_PRIVATE_KEY, CONSOLE_GATEWAY_HTTP_URL, "
                "CONSOLE_GATEWAY_WS_URL",
            )
        if release is None or self._private_key is None or self._gateway is None:
            raise NoRunningDeploymentError("no running deployment", service_id=service.id)

        session_id = str(uuid4())
        token, expires_at = ConsoleTicketSigner(self._private_key).sign(
            session_id, owner_id, service.id, target.id, CONSOLE_CLUSTER_AWS
        )
        await self._console_session_repository.add(
            ConsoleSession(
                public_id=session_id,
                user_id=owner_id,
                service_id=service.id,
                target_id=target.id,
                release_id=release.id,
                expires_at=expires_at,
            )
        )
        await self._session.commit()
        logger.info(
            "console ticket issued",
            extra={
                "action": "create_session",
                "session_id": session_id,
                "user_id": owner_id,
                "service_id": service.id,
                "target_id": target.id,
                "release_id": release.id,
            },
        )
        return IssuedConsoleSession(session_id, token, expires_at, self._gateway)

    async def _get_scope(
        self, owner_id: int, service_id: int, target_id: int
    ) -> tuple[Service, Target]:
        service = await self._service_repository.find_by_id_and_owner_id(service_id, owner_id)
        if service is None:
            raise ServiceNotFoundError("service not found", service_id=service_id)
        target_ids = await self._service_repository.search_target_ids_by_service_ids([service_id])
        if target_id not in target_ids[service_id]:
            raise NotFoundError("target not found", service_id=service_id, target_id=target_id)
        targets = await self._target_repository.search_by_ids([target_id])
        if not targets:
            raise NotFoundError("target not found", service_id=service_id, target_id=target_id)
        return service, targets[0]

    async def _check(
        self, service: Service, target: Target
    ) -> tuple[ConsoleUnavailableReason | None, Release | None]:
        """콘솔을 열 수 없는 사유와 떠 있는 release. 타깃 종류 → 설정 → release 순으로 본다."""
        if target.kind != TargetKind.AWS:
            return ConsoleUnavailableReason.TARGET_NOT_SUPPORTED, None
        if self._private_key is None or self._gateway is None:
            return ConsoleUnavailableReason.NOT_CONFIGURED, None
        release = await self._release_repository.find_last_known_good(service.id, target.id)
        if release is None:
            return ConsoleUnavailableReason.NO_RUNNING_DEPLOYMENT, None
        return None, release
