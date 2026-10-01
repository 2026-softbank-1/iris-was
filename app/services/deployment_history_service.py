from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from app.core.exceptions import DeploymentRequestNotFoundError, ServiceNotFoundError
from app.enums import DeploymentStatus
from app.models.deployment_request import DeploymentRequest
from app.models.deployment_status_history import DeploymentStatusHistory
from app.models.service import Service
from app.repositories.deployment_request_repository import DeploymentRequestRepository
from app.repositories.deployment_status_history_repository import (
    DeploymentStatusHistoryRepository,
)
from app.repositories.service_repository import ServiceRepository


@dataclass(frozen=True)
class DeploymentStage:
    """한 상태에 머문 구간. 아직 머무는 중이거나 마지막(끝난) 상태면 finished_at 이 없다."""

    status: DeploymentStatus
    started_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True)
class DeploymentDetail:
    deployment_request: DeploymentRequest
    histories: list[DeploymentStatusHistory]
    stages: list[DeploymentStage]


@dataclass(frozen=True)
class DeploymentRequestPage:
    items: list[DeploymentRequest]
    total: int


def build_stages(
    deployment_request: DeploymentRequest, histories: Sequence[DeploymentStatusHistory]
) -> list[DeploymentStage]:
    """전이 이력의 시각으로 상태별 구간을 만든다. 구간은 다음 전이가 일어난 시각에 끝난다.

    이력이 없는 요청(이력 도입 전에 만든 요청)은 요청 시각부터 현재 상태 하나로 본다.
    """
    entries = [(h.to_status, h.created_at) for h in histories] or [
        (deployment_request.status, deployment_request.created_at)
    ]
    return [
        DeploymentStage(
            status=status,
            started_at=started_at,
            finished_at=entries[index + 1][1] if index + 1 < len(entries) else None,
        )
        for index, (status, started_at) in enumerate(entries)
    ]


class DeploymentHistoryService:
    """서비스의 배포 요청 목록과 상세(상태 이력·단계별 소요 시간)를 읽는다."""

    def __init__(
        self,
        service_repository: ServiceRepository,
        deployment_request_repository: DeploymentRequestRepository,
        deployment_status_history_repository: DeploymentStatusHistoryRepository,
    ) -> None:
        self._service_repository = service_repository
        self._deployment_request_repository = deployment_request_repository
        self._deployment_status_history_repository = deployment_status_history_repository

    async def search_deployment_requests(
        self, owner_id: int, service_id: int, page: int, size: int
    ) -> DeploymentRequestPage:
        """최신순. 첫 항목이 가장 최근(현재) 배포다."""
        service = await self._get_owned(owner_id, service_id)
        items = await self._deployment_request_repository.search_by_service_id(
            service.id, page, size
        )
        total = await self._deployment_request_repository.count_by_service_id(service.id)
        return DeploymentRequestPage(items=items, total=total)

    async def get_deployment_request(
        self, owner_id: int, service_id: int, deployment_request_id: int
    ) -> DeploymentDetail:
        service = await self._get_owned(owner_id, service_id)
        request = await self._deployment_request_repository.find_by_id_and_service_id(
            deployment_request_id, service.id
        )
        if request is None:
            raise DeploymentRequestNotFoundError(
                "deployment request not found",
                service_id=service.id,
                deployment_request_id=deployment_request_id,
            )
        histories = (
            await self._deployment_status_history_repository.search_by_deployment_request_id(
                request.id
            )
        )
        return DeploymentDetail(request, histories, build_stages(request, histories))

    async def _get_owned(self, owner_id: int, service_id: int) -> Service:
        service = await self._service_repository.find_by_id_and_owner_id(service_id, owner_id)
        if service is None:
            raise ServiceNotFoundError("service not found", service_id=service_id)
        return service
