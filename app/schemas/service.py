from datetime import datetime
from typing import Annotated, Any

from pydantic import Field, StringConstraints

from app.enums import (
    AnalysisGateComplexity,
    AnalysisGateDecision,
    Builder,
    DeploymentStatus,
    DeploymentStrategy,
    DeploymentTrigger,
    FailureCode,
)
from app.models.deployment_request import DeploymentRequest
from app.models.target import Target
from app.schemas.response import ApiModel
from app.services.service_registry_service import ServiceDetail

ServiceName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=63)]
Branch = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
PathText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=255)]
Command = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
Port = Annotated[int, Field(ge=1, le=65535)]


class ServiceCreateRequest(ApiModel):
    """저장소를 연결해 서비스를 만든다. 이름·브랜치를 생략하면 저장소 이름·기본 브랜치를 쓴다."""

    repository_url: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    name: ServiceName | None = None
    branch: Branch | None = None
    root_directory: PathText | None = None
    is_auto_deploy: bool = True
    target_ids: list[int] | None = Field(
        default=None,
        description="배포 타깃 id. 정확히 1개다. 생략하면 `aws` 타깃이다.",
        examples=[[1]],
    )
    analysis_id: int | None = Field(
        default=None,
        description=(
            "같은 저장소·rootDirectory 를 분석해 끝난(SUCCEEDED) 레포 구성 분석 id. 결정을"
            " `analysisGate` 로 남기고, 분석 생략(skip)이면 분석기가 고른 빌더·Dockerfile 경로를"
            " 기본값으로 쓴다. 다른 저장소·위치의 분석이면 422, 끝나지 않았으면 409."
        ),
    )


class ServiceUpdateRequest(ApiModel):
    """보낸 필드만 바꾼다. 명시한 null 은 값을 비운다(name·sourceBranch·isAutoDeploy 제외)."""

    name: ServiceName | None = None
    source_branch: Branch | None = None
    root_directory: PathText | None = None
    is_auto_deploy: bool | None = None
    builder: Builder | None = None
    dockerfile_path: PathText | None = None
    port: Port | None = None
    build_command: Command | None = None
    start_command: Command | None = None
    target_ids: list[int] | None = Field(
        default=None,
        description=(
            "배포 타깃 id. 정확히 1개다. 배포 요청이 한 번이라도 있으면 바꿀 수 없다(`409`)."
            " 바꾸려면 서비스를 지우고 다시 만든다."
        ),
        examples=[[1]],
    )
    deployment_strategy: DeploymentStrategy | None = Field(
        default=None,
        description=(
            "배포 방식. 저장만 하고 배포를 만들지 않으며 다음 배포부터 적용된다."
            " CANARY·BLUE_GREEN 은 기능이 켜져 있고, 타깃이 AWS 이고(on-prem 은 롤링만),"
            " 저장된 Pod 수(`/scaling` 의 replicas, 없으면 1)가 2 이상이어야 한다. 아니면"
            " `422 INVALID_INPUT`(`details[].field = deploymentStrategy`, reason"
            " `deployment_strategy_disabled`·`deployment_strategy_unsupported_target`·"
            "`at_least_two_replicas_required`)."
        ),
        examples=["CANARY"],
    )


class LatestDeploymentResponse(ApiModel):
    """서비스 카드에 보여줄 가장 최근 배포 요청. 서비스 상태는 `status` 로 읽는다."""

    id: int
    status: DeploymentStatus
    source_sha: str
    source_commit_message: str | None = None
    trigger_type: DeploymentTrigger
    failure_code: FailureCode | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, request: DeploymentRequest) -> "LatestDeploymentResponse":
        return cls(
            id=request.id,
            status=request.status,
            source_sha=request.source_sha,
            source_commit_message=request.source_commit_message,
            trigger_type=request.trigger_type,
            failure_code=request.failure_code,
            created_at=request.created_at,
            updated_at=request.updated_at,
        )


class ServiceAnalysisGateResponse(ApiModel):
    """서비스를 만들 때 쓴 레포 구성 분석. unitId 가 있으면 분석 결과의 배포 단위로 만들었다."""

    analysis_id: int
    decision: AnalysisGateDecision | None = None
    complexity: AnalysisGateComplexity | None = None
    unit_id: str | None = None

    @classmethod
    def from_analysis_plan(
        cls, analysis_plan: dict[str, Any] | None
    ) -> "ServiceAnalysisGateResponse | None":
        gate = (analysis_plan or {}).get("gate")
        if not isinstance(gate, dict) or gate.get("analysisId") is None:
            return None
        return cls(
            analysis_id=gate["analysisId"],
            decision=gate.get("decision"),
            complexity=gate.get("complexity"),
            unit_id=gate.get("unitId"),
        )


class ServiceResponse(ApiModel):
    id: int
    project_id: int
    name: str
    source_repository_url: str
    source_branch: str
    root_directory: str | None = None
    is_auto_deploy: bool
    builder: Builder | None = None
    dockerfile_path: str | None = None
    platform: str
    port: int | None = None
    build_command: str | None = None
    start_command: str | None = None
    target_ids: list[int]
    deployment_strategy: DeploymentStrategy = Field(
        description=(
            "다음 배포부터 쓸 배포 방식. Pod 가 2개 미만이거나 타깃이 on-prem 이면 배포할 때"
            " ROLLING 으로 대체된다."
        ),
        examples=["ROLLING"],
    )
    latest_deployment: LatestDeploymentResponse | None = Field(
        default=None, description="가장 최근 배포 요청. 배포한 적이 없으면 없다."
    )
    analysis_gate: ServiceAnalysisGateResponse | None = Field(
        default=None,
        description="레포 구성 분석으로 만든 서비스면 그 분석. 분석 없이 만들었으면 없다.",
    )
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_detail(cls, detail: ServiceDetail) -> "ServiceResponse":
        service = detail.service
        return cls(
            id=service.id,
            project_id=service.project_id,
            name=service.name,
            source_repository_url=service.source_repository_url,
            source_branch=service.source_branch,
            root_directory=service.root_directory,
            is_auto_deploy=service.is_auto_deploy,
            builder=service.builder,
            dockerfile_path=service.dockerfile_path,
            platform=service.platform,
            port=service.port,
            build_command=service.build_command,
            start_command=service.start_command,
            target_ids=detail.target_ids,
            deployment_strategy=service.deployment_strategy,
            latest_deployment=(
                LatestDeploymentResponse.from_model(detail.latest_deployment)
                if detail.latest_deployment is not None
                else None
            ),
            analysis_gate=ServiceAnalysisGateResponse.from_analysis_plan(service.analysis_plan),
            created_at=service.created_at,
            updated_at=service.updated_at,
        )


class TargetResponse(ApiModel):
    id: int
    name: str
    kind: str
    region: str | None = None
    domain_suffix: str | None = None

    @classmethod
    def from_model(cls, target: Target) -> "TargetResponse":
        return cls(
            id=target.id,
            name=target.name,
            kind=target.kind.value,
            region=target.region,
            domain_suffix=target.domain_suffix,
        )
