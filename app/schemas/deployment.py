from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from app.core.exceptions import InvalidInputError
from app.enums import (
    ACTIVE_DEPLOYMENT_STATUSES,
    Builder,
    BuildStatus,
    DeploymentStatus,
    DeploymentTrigger,
    FailureCode,
    ReleaseStatus,
    TargetKind,
)
from app.models.build import Build
from app.models.deployment_request import DeploymentRequest
from app.models.deployment_status_history import DeploymentStatusHistory
from app.models.release import Release
from app.models.service import Service
from app.models.target import Target
from app.schemas.response import ApiModel
from app.services.deployment_history_service import (
    DeploymentDetail,
    DeploymentReplacement,
    DeploymentStage,
)
from app.services.repository_url import parse_repository_url

CommitSha = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=7, max_length=64, pattern=r"^[0-9a-fA-F]+$"
    ),
]


class CreateDeploymentRequest(ApiModel):
    """배포 요청을 직접 만든다. 푸시 웹훅이 못 하는 첫 배포·재배포·롤백·재시작·삭제에 쓴다."""

    trigger_type: Literal[
        DeploymentTrigger.MANUAL,
        DeploymentTrigger.REDEPLOY,
        DeploymentTrigger.ROLLBACK,
        DeploymentTrigger.RESTART,
        DeploymentTrigger.REMOVE,
    ] = Field(
        description=(
            "MANUAL: 브랜치 최신 커밋(또는 sourceSha)을 빌드해 배포한다. "
            "REDEPLOY: sourceDeploymentId 의 커밋을 다시 빌드해 배포한다. "
            "ROLLBACK: 성공했던 sourceDeploymentId 가 만든 이미지를 빌드 없이 그대로 배포한다. "
            "RESTART: 지금 떠 있는(마지막으로 성공한) 배포의 이미지를 빌드 없이 다시 띄워 "
            "Pod 를 새로 시작한다. "
            "REMOVE: 지금 떠 있는 배포를 클러스터에서 내린다(서비스 정의는 남는다). "
            "다시 배포하려면 MANUAL·REDEPLOY·ROLLBACK 을 쓴다."
        ),
        examples=["MANUAL"],
    )
    source_sha: CommitSha | None = Field(
        default=None,
        description="MANUAL 에서만 쓴다. 없으면 서비스 브랜치의 최신 커밋을 서버가 조회한다.",
        examples=["9f2c1ab"],
    )
    source_deployment_id: int | None = Field(
        default=None,
        description=(
            "REDEPLOY·ROLLBACK 에서 필수. 같은 서비스의 이전 배포 id. "
            "ROLLBACK 은 SUCCEEDED 여야 한다. MANUAL·RESTART·REMOVE 에서는 보내지 않는다."
        ),
        examples=[12],
    )

    @model_validator(mode="after")
    def check_source_fields(self) -> Self:
        takes_source_deployment = self.trigger_type in (
            DeploymentTrigger.REDEPLOY,
            DeploymentTrigger.ROLLBACK,
        )
        if takes_source_deployment and self.source_deployment_id is None:
            raise ValueError("sourceDeploymentId is required for REDEPLOY and ROLLBACK")
        if not takes_source_deployment and self.source_deployment_id is not None:
            raise ValueError("sourceDeploymentId is only for REDEPLOY and ROLLBACK")
        if self.trigger_type != DeploymentTrigger.MANUAL and self.source_sha is not None:
            raise ValueError("sourceSha is only for MANUAL")
        return self


class DeploymentResponse(ApiModel):
    """배포 요청 1건. 상태 이름: QUEUED=Initializing, SUCCEEDED=Active."""

    id: int
    service_id: int
    status: DeploymentStatus
    source_sha: str
    source_commit_message: str | None = None
    trigger_type: DeploymentTrigger
    source_deployment_id: int | None = Field(
        default=None,
        description=(
            "재배포·롤백·재시작·삭제가 따라간 원본 배포 id. "
            "롤백·재시작은 이 배포가 만든 이미지를 쓰고, 삭제는 이 배포를 내린다."
        ),
    )
    failure_code: FailureCode | None = Field(
        default=None, description="FAILED 일 때만 있다. 원인 구분은 상태가 아니라 이 코드로 한다."
    )
    requested_by: int | None = Field(
        default=None, description="요청한 사용자 id. 푸시 웹훅이 만든 요청은 없다."
    )
    is_active: bool = Field(description="QUEUED·BUILDING·DEPLOYING 이면 진행 중이다.")
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, request: DeploymentRequest) -> "DeploymentResponse":
        return DeploymentResponse(
            id=request.id,
            service_id=request.service_id,
            status=request.status,
            source_sha=request.source_sha,
            source_commit_message=request.source_commit_message,
            trigger_type=request.trigger_type,
            source_deployment_id=request.source_deployment_request_id,
            failure_code=request.failure_code,
            requested_by=request.requested_by,
            is_active=request.status in ACTIVE_DEPLOYMENT_STATUSES,
            created_at=request.created_at,
            updated_at=request.updated_at,
        )


class DeploymentStatusHistoryResponse(ApiModel):
    """상태가 바뀐 기록 1건. 첫 기록은 fromStatus 가 없다."""

    from_status: DeploymentStatus | None = None
    to_status: DeploymentStatus
    failure_code: FailureCode | None = None
    created_at: datetime = Field(description="상태가 바뀐 시각")

    @classmethod
    def from_model(cls, history: DeploymentStatusHistory) -> Self:
        return cls(
            from_status=history.from_status,
            to_status=history.to_status,
            failure_code=history.failure_code,
            created_at=history.created_at,
        )


class DeploymentStageResponse(ApiModel):
    """한 상태에 머문 구간. 마지막 구간은 finishedAt·durationSeconds 가 없다."""

    status: DeploymentStatus
    started_at: datetime
    finished_at: datetime | None = None
    duration_seconds: float | None = Field(
        default=None, description="finishedAt - startedAt (초). 아직 끝나지 않았으면 없다."
    )

    @classmethod
    def from_stage(cls, stage: DeploymentStage) -> Self:
        duration = (
            round((stage.finished_at - stage.started_at).total_seconds(), 3)
            if stage.finished_at is not None
            else None
        )
        return cls(
            status=stage.status,
            started_at=stage.started_at,
            finished_at=stage.finished_at,
            duration_seconds=duration,
        )


class DeploymentSourceResponse(ApiModel):
    """배포한 소스. 커밋 SHA·메시지는 배포 요청 최상위 필드(sourceSha·sourceCommitMessage)다."""

    repository: str = Field(
        description="owner/repo 형식의 소스 저장소", examples=["Saccharine1211/railway-deploy-demo"]
    )
    branch: str = Field(
        description="서비스에 지정된 배포 브랜치. 배포 시점이 아니라 지금의 설정값이다.",
        examples=["main"],
    )

    @classmethod
    def from_service(cls, service: Service) -> Self:
        try:
            owner, name = parse_repository_url(service.source_repository_url)
            repository = f"{owner}/{name}"
        except InvalidInputError:
            repository = service.source_repository_url
        return cls(repository=repository, branch=service.source_branch)


class DeploymentTargetResponse(ApiModel):
    id: int
    name: str = Field(examples=["aws"])
    kind: TargetKind

    @classmethod
    def from_model(cls, target: Target) -> Self:
        return cls(id=target.id, name=target.name, kind=target.kind)


class DeploymentBuildConfigurationResponse(ApiModel):
    builder: Builder | None = Field(
        default=None, description="없으면 빌더를 아직 확정하지 않은 것이다(화면의 Auto-detect)."
    )
    root_directory: str | None = Field(
        default=None, description="저장소 안의 서비스 위치. 없으면 저장소 루트다."
    )
    build_command: str | None = None


class DeploymentDeployConfigurationResponse(ApiModel):
    targets: list[DeploymentTargetResponse] = Field(
        description="실제로 반영한 타깃. 아직 반영 전이면 서비스에 지정된 타깃이다."
    )
    port: int | None = None
    start_command: str | None = Field(
        default=None, description="빌드가 기록한 값이 있으면 그 값, 없으면 서비스 설정값이다."
    )


class DeploymentConfigurationResponse(ApiModel):
    """화면의 Configuration. root·build command·port 는 배포 시점이 아닌 현재 설정값이다."""

    build: DeploymentBuildConfigurationResponse
    deploy: DeploymentDeployConfigurationResponse

    @classmethod
    def from_detail(cls, detail: DeploymentDetail) -> Self:
        service, build = detail.service, detail.build
        deploy_config = (build.deploy_config if build is not None else None) or {}
        return cls(
            build=DeploymentBuildConfigurationResponse(
                builder=(build.builder if build is not None else None) or service.builder,
                root_directory=service.root_directory,
                build_command=service.build_command,
            ),
            deploy=DeploymentDeployConfigurationResponse(
                targets=[DeploymentTargetResponse.from_model(t) for t in detail.targets],
                port=service.port,
                start_command=deploy_config.get("startCommand") or service.start_command,
            ),
        )


class DeploymentBuildResponse(ApiModel):
    """이 배포 요청의 빌드 결과. 롤백·재시작은 원본 빌드를 복사한 것이라 시각이 요청 시각이다."""

    status: BuildStatus
    builder: Builder | None = None
    image_digest: str | None = Field(default=None, examples=["sha256:3f1c..."])
    started_at: datetime | None = None
    finished_at: datetime | None = None
    failure_code: FailureCode | None = None

    @classmethod
    def from_model(cls, build: Build) -> Self:
        return cls(
            status=build.status,
            builder=build.builder,
            image_digest=build.image_digest,
            started_at=build.started_at,
            finished_at=build.finished_at,
            failure_code=build.failure_code,
        )


class DeploymentReleaseResponse(ApiModel):
    """타깃 하나에 반영한 결과. Argo CD 상태는 원본 표기 그대로다."""

    id: int
    target_id: int
    status: ReleaseStatus
    argo_sync_status: str | None = Field(default=None, examples=["Synced"])
    argo_health_status: str | None = Field(default=None, examples=["Healthy"])
    gitops_commit_sha: str | None = None
    failure_code: FailureCode | None = None
    finished_at: datetime | None = None

    @classmethod
    def from_model(cls, release: Release) -> Self:
        return cls(
            id=release.id,
            target_id=release.target_id,
            status=release.status,
            argo_sync_status=release.argo_sync_status,
            argo_health_status=release.argo_health_status,
            gitops_commit_sha=release.gitops_commit_sha,
            failure_code=release.failure_code,
            finished_at=release.finished_at,
        )


class DeploymentReplacedByResponse(ApiModel):
    deployment_id: int = Field(description="이 배포를 대신한 더 새로운 성공 배포 id")
    at: datetime = Field(description="그 배포가 성공한 시각")

    @classmethod
    def from_replacement(cls, replacement: DeploymentReplacement) -> Self:
        return cls(deployment_id=replacement.deployment_request_id, at=replacement.at)


class DeploymentDetailResponse(DeploymentResponse):
    history: list[DeploymentStatusHistoryResponse]
    stages: list[DeploymentStageResponse]
    source: DeploymentSourceResponse
    configuration: DeploymentConfigurationResponse
    build: DeploymentBuildResponse | None = Field(
        default=None, description="빌드를 시작하기 전에 끝난 요청은 없다."
    )
    releases: list[DeploymentReleaseResponse] = Field(
        description="빌드 실패처럼 배포까지 가지 못한 요청은 비어 있다."
    )
    replaced_by: DeploymentReplacedByResponse | None = Field(
        default=None,
        description="성공했던 배포를 더 새로운 성공 배포가 대신했을 때만 있다(화면의 Removed).",
    )

    @classmethod
    def from_detail(cls, detail: DeploymentDetail) -> Self:
        base = DeploymentResponse.from_model(detail.deployment_request)
        return cls(
            **base.model_dump(),
            history=[DeploymentStatusHistoryResponse.from_model(h) for h in detail.histories],
            stages=[DeploymentStageResponse.from_stage(s) for s in detail.stages],
            source=DeploymentSourceResponse.from_service(detail.service),
            configuration=DeploymentConfigurationResponse.from_detail(detail),
            build=DeploymentBuildResponse.from_model(detail.build) if detail.build else None,
            releases=[DeploymentReleaseResponse.from_model(r) for r in detail.releases],
            replaced_by=(
                DeploymentReplacedByResponse.from_replacement(detail.replaced_by)
                if detail.replaced_by
                else None
            ),
        )
