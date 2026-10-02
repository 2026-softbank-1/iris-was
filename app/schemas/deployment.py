from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from app.enums import ACTIVE_DEPLOYMENT_STATUSES, DeploymentStatus, DeploymentTrigger, FailureCode
from app.models.deployment_request import DeploymentRequest
from app.models.deployment_status_history import DeploymentStatusHistory
from app.schemas.response import ApiModel
from app.services.deployment_history_service import DeploymentDetail, DeploymentStage

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


class DeploymentDetailResponse(DeploymentResponse):
    history: list[DeploymentStatusHistoryResponse]
    stages: list[DeploymentStageResponse]

    @classmethod
    def from_detail(cls, detail: DeploymentDetail) -> Self:
        base = DeploymentResponse.from_model(detail.deployment_request)
        return cls(
            **base.model_dump(),
            history=[DeploymentStatusHistoryResponse.from_model(h) for h in detail.histories],
            stages=[DeploymentStageResponse.from_stage(s) for s in detail.stages],
        )
