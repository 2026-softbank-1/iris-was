import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    DeploymentInProgressError,
    DeploymentRequestNotFoundError,
    InvalidInputError,
    NoSucceededDeploymentError,
    ServiceNotFoundError,
    UploadNotFoundError,
    UploadUnavailableError,
)
from app.enums import BuildStatus, DeploymentStatus, DeploymentTrigger
from app.models.build import Build
from app.models.deployment_request import DeploymentRequest
from app.models.service import Service
from app.models.service_upload import ServiceUpload
from app.repositories.build_repository import BuildRepository
from app.repositories.deployment_request_repository import DeploymentRequestRepository
from app.repositories.service_repository import ServiceRepository
from app.repositories.service_upload_repository import ServiceUploadRepository
from app.services.deployment_request_service import DeploymentRequestService
from app.services.repository_url import parse_repository_url
from app.services.source_repository_service import SourceRepositoryService
from app.services.upload_source import build_upload_source_sha, is_upload_source_sha

logger = logging.getLogger(__name__)

# 원본 배포 요청의 커밋을 가져오는 요청. 사용자가 원본을 고른다.
_SOURCE_COPYING_TRIGGERS = frozenset({DeploymentTrigger.REDEPLOY, DeploymentTrigger.ROLLBACK})
# 원본이 만든 이미지를 빌드 없이 다시 배포하는 요청.
_IMAGE_REUSING_TRIGGERS = frozenset({DeploymentTrigger.ROLLBACK, DeploymentTrigger.RESTART})
# 지금 떠 있는(마지막으로 성공한) 배포를 대상으로 하는 요청.
_LIVE_DEPLOYMENT_TRIGGERS = frozenset({DeploymentTrigger.RESTART, DeploymentTrigger.REMOVE})
_MANUAL_TRIGGERS = (
    frozenset({DeploymentTrigger.MANUAL, DeploymentTrigger.CLI})
    | _SOURCE_COPYING_TRIGGERS
    | _IMAGE_REUSING_TRIGGERS
    | _LIVE_DEPLOYMENT_TRIGGERS
)


class ManualDeploymentService:
    """사용자가 직접 만드는 배포 요청: 첫 배포·CLI 업로드·재배포·롤백·재시작·삭제. 웹훅과 공유한다.

    CLI 는 `likelion up` 이 올린 아카이브를 GitHub 대신 소스로 빌드한다. 재배포는 같은 커밋을
    다시 빌드한다. 롤백과 재시작은 이미 빌드한 이미지를 그대로 다시 배포한다. 삭제는 지금 떠 있는
    배포를 클러스터에서 내린다.
    """

    def __init__(
        self,
        session: AsyncSession,
        service_repository: ServiceRepository,
        deployment_request_repository: DeploymentRequestRepository,
        build_repository: BuildRepository,
        deployment_request_service: DeploymentRequestService,
        source_repository_service: SourceRepositoryService,
        upload_repository: ServiceUploadRepository,
    ) -> None:
        self._session = session
        self._service_repository = service_repository
        self._deployment_request_repository = deployment_request_repository
        self._build_repository = build_repository
        self._deployment_request_service = deployment_request_service
        self._source_repository_service = source_repository_service
        self._upload_repository = upload_repository

    async def create_deployment_request(
        self,
        owner_id: int,
        service_id: int,
        *,
        trigger_type: DeploymentTrigger,
        source_sha: str | None = None,
        source_deployment_request_id: int | None = None,
        upload_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> DeploymentRequest:
        """같은 `idempotency_key` 로 다시 요청하면 처음 만든 배포 요청을 그대로 돌려준다.

        CLI 는 `upload_id` 의 업로드를 이 요청에 묶는다. 묶는 일과 요청 생성은 한 트랜잭션이라,
        요청을 만들지 못하면(진행 중인 배포가 있으면) 업로드는 다시 쓸 수 있다.
        """
        if trigger_type not in _MANUAL_TRIGGERS:
            raise InvalidInputError("trigger type is not allowed here", trigger_type=trigger_type)
        service = await self._get_owned(owner_id, service_id)
        # 전역으로 유일한 키라서 서비스 id 를 붙여 다른 서비스의 키와 섞이지 않게 한다.
        key = f"manual:{service.id}:{idempotency_key or uuid.uuid4()}"

        if trigger_type == DeploymentTrigger.REMOVE:
            live = await self._get_live_deployment(service)
            request = await self._deployment_request_service.create_removal_request(
                service,
                source_deployment_request=live,
                idempotency_key=key,
                requested_by=owner_id,
            )
        elif trigger_type == DeploymentTrigger.CLI:
            # 같은 키의 재시도는 이미 쓰인 업로드를 다시 가져가려 하므로, 가져가기 전에 먼저 찾는다.
            replayed = await self._deployment_request_repository.find_by_idempotency_key(key)
            if replayed is not None:
                return replayed
            upload = await self._claim_upload(service, upload_id)
            request = await self._deployment_request_service.create_deployment_request(
                service,
                source_sha=build_upload_source_sha(upload.sha256),
                source_commit_message=None,
                trigger_type=trigger_type,
                idempotency_key=key,
                requested_by=owner_id,
                service_upload_id=upload.id,
            )
        elif trigger_type in _IMAGE_REUSING_TRIGGERS:
            source = await self._find_image_source(
                service, trigger_type, source_deployment_request_id
            )
            source_build = await self._get_reusable_build(source)
            request = (
                await self._deployment_request_service.create_deployment_request_reusing_image(
                    service,
                    source_deployment_request=source,
                    source_build=source_build,
                    trigger_type=trigger_type,
                    idempotency_key=key,
                    requested_by=owner_id,
                )
            )
        else:
            source = None
            if trigger_type in _SOURCE_COPYING_TRIGGERS:
                source = await self._get_source(service, trigger_type, source_deployment_request_id)
                commit_sha, commit_message = source.source_sha, source.source_commit_message
            else:
                commit_sha, commit_message = await self._resolve_head(owner_id, service, source_sha)
            request = await self._deployment_request_service.create_deployment_request(
                service,
                source_sha=commit_sha,
                source_commit_message=commit_message,
                trigger_type=trigger_type,
                idempotency_key=key,
                requested_by=owner_id,
                source_deployment_request=source,
            )
        if request is None:
            if trigger_type == DeploymentTrigger.CLI:
                # 요청을 만들지 못했으니 가져간 업로드를 되돌린다. 세션이 닫히며 되돌려지지만
                # 아래에서 같은 세션으로 더 읽으므로 먼저 끝낸다. 롤백은 세션의 객체를 만료시켜
                # `service` 를 더 읽을 수 없다.
                await self._session.rollback()
            replayed = await self._deployment_request_repository.find_by_idempotency_key(key)
            if replayed is None:
                raise DeploymentInProgressError(
                    "a deployment is already in progress", service_id=service_id
                )
            return replayed

        await self._session.commit()
        logger.info(
            "manual deployment requested",
            extra={
                "action": "create_deployment_request",
                "service_id": service.id,
                "deployment_request_id": request.id,
                "trigger_type": trigger_type,
            },
        )
        return request

    async def _claim_upload(self, service: Service, upload_id: str | None) -> ServiceUpload:
        """업로드를 이 요청의 소스로 가져간다. 모르는·다른 서비스의 업로드는 같은 404 다."""
        if upload_id is None:
            raise InvalidInputError("upload is required", field="uploadId")
        upload = await self._upload_repository.find_by_public_id_and_service_id(
            upload_id, service.id
        )
        if upload is None:
            raise UploadNotFoundError("upload not found", service_id=service.id)
        if not await self._upload_repository.claim(upload.id, datetime.now(UTC)):
            raise UploadUnavailableError(
                "upload is already used or expired", service_id=service.id, upload_id=upload.id
            )
        return upload

    async def _find_image_source(
        self,
        service: Service,
        trigger_type: DeploymentTrigger,
        source_deployment_request_id: int | None,
    ) -> DeploymentRequest:
        """롤백은 사용자가 고른 성공한 배포, 재시작은 지금 떠 있는(마지막으로 성공한) 배포다."""
        if trigger_type == DeploymentTrigger.ROLLBACK:
            return await self._get_source(service, trigger_type, source_deployment_request_id)
        return await self._get_live_deployment(service)

    async def _get_live_deployment(self, service: Service) -> DeploymentRequest:
        """지금 떠 있는 배포. 성공한 배포가 없거나 마지막 성공이 서비스를 내린 요청이면 없다."""
        live = await self._deployment_request_repository.find_latest_succeeded_by_service_id(
            service.id
        )
        if live is None or live.trigger_type == DeploymentTrigger.REMOVE:
            raise NoSucceededDeploymentError("no running deployment", service_id=service.id)
        return live

    async def _get_reusable_build(self, source: DeploymentRequest) -> Build:
        build = await self._build_repository.find_by_deployment_request_id(source.id)
        if build is None or build.status != BuildStatus.SUCCEEDED or build.image_digest is None:
            raise InvalidInputError(
                "source deployment has no built image",
                field="sourceDeploymentId",
                deployment_request_id=source.id,
            )
        return build

    async def _get_source(
        self,
        service: Service,
        trigger_type: DeploymentTrigger,
        source_deployment_request_id: int | None,
    ) -> DeploymentRequest:
        if source_deployment_request_id is None:
            raise InvalidInputError(
                "source deployment is required",
                field="sourceDeploymentId",
                trigger_type=trigger_type,
            )
        source = await self._deployment_request_repository.find_by_id_and_service_id(
            source_deployment_request_id, service.id
        )
        if source is None:
            raise DeploymentRequestNotFoundError(
                "source deployment not found",
                service_id=service.id,
                deployment_request_id=source_deployment_request_id,
            )
        if trigger_type == DeploymentTrigger.REDEPLOY and is_upload_source_sha(source.source_sha):
            # 업로드한 소스는 빌드 입력으로만 잠시 남고 GitHub 에서 다시 받을 수도 없다.
            raise InvalidInputError(
                "source deployment was built from a CLI upload and cannot be rebuilt",
                field="sourceDeploymentId",
                deployment_request_id=source.id,
            )
        if (
            trigger_type == DeploymentTrigger.ROLLBACK
            and source.status != DeploymentStatus.SUCCEEDED
        ):
            raise InvalidInputError(
                "rollback target must be a succeeded deployment",
                field="sourceDeploymentId",
                deployment_request_id=source.id,
                status=source.status,
            )
        return source

    async def _resolve_head(
        self, owner_id: int, service: Service, source_sha: str | None
    ) -> tuple[str, str | None]:
        """`source_sha` 가 없으면 서비스 브랜치의 최신 커밋을 GitHub 에서 읽는다."""
        if source_sha is not None:
            return source_sha, None
        owner, name = parse_repository_url(service.source_repository_url)
        head = await self._source_repository_service.find_branch_head(
            owner_id, f"{owner}/{name}", service.source_branch
        )
        if head is None:
            raise InvalidInputError(
                "branch not found in repository", field="branch", branch=service.source_branch
            )
        return head.sha, head.message

    async def _get_owned(self, owner_id: int, service_id: int) -> Service:
        service = await self._service_repository.find_by_id_and_owner_id(service_id, owner_id)
        if service is None:
            raise ServiceNotFoundError("service not found", service_id=service_id)
        return service
