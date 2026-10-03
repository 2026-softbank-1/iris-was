"""코드 수정에 필요한 입력을 한 번에 고정해 돌려준다: 성공한 진단 원문과 같은 시점의 소스 스냅샷.

수정은 어느 진단의 어느 소스에 대한 것인지가 흔들리면 안 된다. 그래서 진단 행 ID 를 받아 그 배포
요청의 것인지 확인하고, 진단 때 쓴 스냅샷을 찾아 올릴 때 고정한 해시와 함께 돌려준다. 이 서비스는
읽기만 한다. GitHub 쓰기·모델 호출은 하지 않는다.
"""

import logging
import posixpath
import re
from datetime import timedelta

from app.clients.aws_clients import PRESIGNED_URL_SECONDS, SnapshotUrlClient
from app.core.exceptions import (
    DeploymentNotFailedError,
    DeploymentRequestNotFoundError,
    DiagnosisNotFoundError,
    DiagnosisNotSucceededError,
    ServiceNotFoundError,
    SourceSnapshotUnavailableError,
)
from app.enums import DiagnosisStatus
from app.models.base import now_utc
from app.repositories.build_repository import BuildRepository
from app.repositories.deployment_diagnosis_repository import DeploymentDiagnosisRepository
from app.repositories.deployment_request_repository import DeploymentRequestRepository
from app.repositories.service_repository import ServiceRepository
from app.schemas.repair import RepairContextResponse, RepairSource
from app.services.diagnosis_service import DIAGNOSABLE_STATUSES
from app.services.source_snapshot import find_snapshot_build

logger = logging.getLogger(__name__)

_COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class RepairContextService:
    def __init__(
        self,
        service_repository: ServiceRepository,
        deployment_request_repository: DeploymentRequestRepository,
        build_repository: BuildRepository,
        diagnosis_repository: DeploymentDiagnosisRepository,
        snapshot_client: SnapshotUrlClient | None,
    ) -> None:
        self._service_repository = service_repository
        self._deployment_request_repository = deployment_request_repository
        self._build_repository = build_repository
        self._diagnosis_repository = diagnosis_repository
        self._snapshot_client = snapshot_client

    async def get_repair_context(
        self, owner_id: int, service_id: int, deployment_request_id: int, diagnosis_id: int
    ) -> RepairContextResponse:
        service = await self._service_repository.find_by_id_and_owner_id(service_id, owner_id)
        if service is None:
            raise ServiceNotFoundError("service not found", service_id=service_id)
        request = await self._deployment_request_repository.find_by_id_and_service_id(
            deployment_request_id, service.id
        )
        if request is None:
            raise DeploymentRequestNotFoundError(
                "deployment request not found",
                service_id=service_id,
                deployment_request_id=deployment_request_id,
            )
        if request.status not in DIAGNOSABLE_STATUSES:
            raise DeploymentNotFailedError(
                "only failed deployments can be repaired",
                deployment_request_id=request.id,
                deployment_status=request.status,
            )

        diagnosis = await self._diagnosis_repository.get_by_id(diagnosis_id)
        # 다른 배포의 진단 ID 를 넣어 그 결과를 읽는 일을 막는다. 존재 여부도 알리지 않는다.
        if diagnosis.deployment_request_id != request.id:
            raise DiagnosisNotFoundError("diagnosis not found", diagnosis_id=diagnosis_id)
        if diagnosis.status != DiagnosisStatus.SUCCEEDED or diagnosis.result is None:
            raise DiagnosisNotSucceededError(
                "diagnosis has not succeeded",
                diagnosis_id=diagnosis.id,
                diagnosis_status=diagnosis.status,
            )

        if self._snapshot_client is None or not _COMMIT_SHA_PATTERN.match(request.source_sha):
            raise SourceSnapshotUnavailableError(
                "source snapshot is unavailable", deployment_request_id=request.id
            )
        build = await self._build_repository.find_by_deployment_request_id(request.id)
        snapshot_build = await find_snapshot_build(self._build_repository, request, build)
        if snapshot_build is None:
            raise SourceSnapshotUnavailableError(
                "source snapshot is unavailable", deployment_request_id=request.id
            )
        download_url = await self._snapshot_client.presign_snapshot(snapshot_build.id)
        logger.info(
            "repair context issued",
            extra={
                "action": "get_repair_context",
                "service_id": service.id,
                "deployment_request_id": request.id,
                "diagnosis_id": diagnosis.id,
                "is_source_pinned": snapshot_build.source_manifest_sha256 is not None,
            },
        )
        return RepairContextResponse(
            repository_url=service.source_repository_url,
            branch=service.source_branch,
            auto_deploy=service.is_auto_deploy,
            deployment_id=request.id,
            diagnosis_id=diagnosis.id,
            source=RepairSource(
                commit_sha=request.source_sha,
                root_directory=_normalize_root(service.root_directory),
                download_url=download_url,
                expires_at=now_utc() + timedelta(seconds=PRESIGNED_URL_SECONDS),
                archive_sha256=snapshot_build.source_archive_sha256,
                manifest_sha256=snapshot_build.source_manifest_sha256,
            ),
            diagnosis_result=diagnosis.result,
        )


def _normalize_root(root_directory: str | None) -> str:
    return posixpath.normpath(root_directory or ".").strip("/") or "."
