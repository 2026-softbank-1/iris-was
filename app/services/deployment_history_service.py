from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from app.core.exceptions import DeploymentRequestNotFoundError, ServiceNotFoundError
from app.enums import DeploymentStatus
from app.models.build import Build
from app.models.deployment_request import DeploymentRequest
from app.models.deployment_status_history import DeploymentStatusHistory
from app.models.release import Release
from app.models.service import Service
from app.models.target import Target
from app.repositories.build_repository import BuildRepository
from app.repositories.deployment_request_repository import DeploymentRequestRepository
from app.repositories.deployment_status_history_repository import (
    DeploymentStatusHistoryRepository,
)
from app.repositories.release_repository import ReleaseRepository
from app.repositories.service_repository import ServiceRepository
from app.repositories.target_repository import TargetRepository


@dataclass(frozen=True)
class DeploymentStage:
    """한 상태에 머문 구간. 아직 머무는 중이거나 마지막(끝난) 상태면 finished_at 이 없다."""

    status: DeploymentStatus
    started_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True)
class DeploymentReplacement:
    """성공한 배포를 대신한 더 새로운 성공 배포와, 그 배포가 성공한 시각."""

    deployment_request_id: int
    at: datetime


@dataclass(frozen=True)
class DeploymentDetail:
    deployment_request: DeploymentRequest
    histories: list[DeploymentStatusHistory]
    stages: list[DeploymentStage]
    service: Service
    # 빌드 전에 실패한 요청은 build·releases 가 없다.
    build: Build | None
    releases: list[Release]
    targets: list[Target]
    replaced_by: DeploymentReplacement | None


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
    """서비스의 배포 요청 목록과 상세(상태 이력·단계별 소요 시간·설정·빌드·릴리스)를 읽는다."""

    def __init__(
        self,
        service_repository: ServiceRepository,
        deployment_request_repository: DeploymentRequestRepository,
        deployment_status_history_repository: DeploymentStatusHistoryRepository,
        build_repository: BuildRepository,
        release_repository: ReleaseRepository,
        target_repository: TargetRepository,
    ) -> None:
        self._service_repository = service_repository
        self._deployment_request_repository = deployment_request_repository
        self._deployment_status_history_repository = deployment_status_history_repository
        self._build_repository = build_repository
        self._release_repository = release_repository
        self._target_repository = target_repository

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
        build = await self._build_repository.find_by_deployment_request_id(request.id)
        releases = await self._release_repository.search_by_deployment_request_id(request.id)
        return DeploymentDetail(
            deployment_request=request,
            histories=histories,
            stages=build_stages(request, histories),
            service=service,
            build=build,
            releases=releases,
            targets=await self._search_targets(service.id, releases),
            replaced_by=await self._find_replacement(request),
        )

    async def _search_targets(self, service_id: int, releases: list[Release]) -> list[Target]:
        """실제로 반영한 타깃. release 가 아직 없으면 서비스에 지정된 타깃을 보여 준다."""
        target_ids = list(dict.fromkeys(release.target_id for release in releases))
        if not target_ids:
            assigned = await self._service_repository.search_target_ids_by_service_ids([service_id])
            target_ids = assigned[service_id]
        if not target_ids:
            return []
        return await self._target_repository.search_by_ids(target_ids)

    async def _find_replacement(self, request: DeploymentRequest) -> DeploymentReplacement | None:
        if request.status != DeploymentStatus.SUCCEEDED:
            return None
        newer = await self._deployment_request_repository.find_first_succeeded_after(
            request.service_id, request.environment, request.id
        )
        if newer is None:
            return None
        histories = (
            await self._deployment_status_history_repository.search_by_deployment_request_id(
                newer.id
            )
        )
        succeeded_at = next(
            (h.created_at for h in histories if h.to_status == DeploymentStatus.SUCCEEDED),
            newer.updated_at,
        )
        return DeploymentReplacement(newer.id, succeeded_at)

    async def _get_owned(self, owner_id: int, service_id: int) -> Service:
        service = await self._service_repository.find_by_id_and_owner_id(service_id, owner_id)
        if service is None:
            raise ServiceNotFoundError("service not found", service_id=service_id)
        return service
