from datetime import datetime
from typing import Annotated, Any

from pydantic import Field, StringConstraints

from app.enums import (
    AnalysisGateComplexity,
    AnalysisGateDecision,
    Builder,
    DatabaseEngine,
    DeploymentStatus,
    DeploymentStrategy,
    DeploymentTrigger,
    FailureCode,
    ReferenceProperty,
    ServiceKind,
)
from app.models.deployment_request import DeploymentRequest
from app.models.target import Target
from app.schemas.response import ApiModel
from app.services.database_engines import get_engine_spec, url_template
from app.services.service_networking import internal_host, internal_port, supported_properties
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


class HostAliasRequest(ApiModel):
    name: Annotated[str, StringConstraints(min_length=1, max_length=63)] = Field(
        description="DNS-1035 레이블(`app` 제외). 이 서비스 안에서 이 이름이 대상 서비스로 풀린다.",
        examples=["api"],
    )
    target_service_id: int = Field(description="같은 프로젝트의 다른 서비스 id", examples=[42])
    port: Port | None = Field(default=None, description="코드가 쓰는 포트(표시용)", examples=[3000])


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
    host_aliases: list[HostAliasRequest] | None = Field(
        default=None,
        description=(
            "호스트 별칭 전체(교체). null·빈 배열이면 모두 지운다. compose 호스트명(`api`)을 같은"
            " 프로젝트 서비스로 잇는다. 기능이 꺼져 있거나 on-prem 타깃이면 `422 INVALID_INPUT`"
            "(reason `project_networking_disabled`·`networking_unsupported_target`)."
        ),
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


class HostAliasResponse(ApiModel):
    name: str
    target_service_id: int
    port: int | None = None


class DatabaseConnectionResponse(ApiModel):
    """관리형 DB 연결 정보. 비밀번호는 `****` 로 가린다. 앱은 참조 변수로 실제 값을 받는다."""

    url_template: str = Field(
        examples=["postgresql://app:****@app.svc-12.svc.cluster.local:5432/app"]
    )
    properties: list[ReferenceProperty] = Field(description="참조 변수로 쓸 수 있는 속성")


class DatabaseSettingsResponse(ApiModel):
    image: str | None = None
    storage_gi: int | None = Field(default=None, description="만든 뒤에는 바꿀 수 없다")
    user: str | None = None
    database: str | None = None


class ServiceStackResponse(ApiModel):
    id: int
    unit_id: str | None = Field(default=None, description="분석기 unit·dependency id")


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
    kind: ServiceKind = Field(
        description="APP(소스를 빌드하는 앱) · DATABASE(고정 공식 이미지의 개발용 DB)"
    )
    database_engine: DatabaseEngine | None = None
    database: DatabaseSettingsResponse | None = Field(
        default=None,
        description="관리형 DB 설정. 데모용 단일 인스턴스, 백업 없음, 삭제 시 데이터 소실",
    )
    internal_host: str = Field(
        description="같은 프로젝트 다른 서비스가 부르는 클러스터 내부 주소",
        examples=["app.svc-12.svc.cluster.local"],
    )
    internal_port: int = Field(description="내부 주소의 포트", examples=[5432])
    connection: DatabaseConnectionResponse | None = Field(
        default=None, description="kind=DATABASE 일 때만 있다"
    )
    reference_properties: list[ReferenceProperty] = Field(
        description="다른 서비스의 참조 변수가 이 서비스에서 가리킬 수 있는 속성"
    )
    host_aliases: list[HostAliasResponse] | None = None
    stack: ServiceStackResponse | None = Field(
        default=None, description="같은 레포 분석에서 함께 만든 스택. 단일 서비스는 없다."
    )
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_detail(cls, detail: ServiceDetail) -> "ServiceResponse":
        service = detail.service
        port = internal_port(service, is_networking=detail.is_networking)
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
            kind=service.kind or ServiceKind.APP,
            database_engine=service.database_engine,
            database=_database_settings(service),
            internal_host=internal_host(service.id),
            internal_port=port,
            connection=_connection(service, port),
            reference_properties=supported_properties(service),
            host_aliases=(
                [
                    HostAliasResponse(
                        name=str(a.get("name")),
                        target_service_id=int(a.get("targetServiceId") or 0),
                        port=a.get("port"),
                    )
                    for a in service.host_aliases
                ]
                if service.host_aliases
                else None
            ),
            stack=(
                ServiceStackResponse(id=service.stack_id, unit_id=service.stack_unit_id)
                if service.stack_id is not None
                else None
            ),
            created_at=service.created_at,
            updated_at=service.updated_at,
        )


def _database_settings(service: Any) -> DatabaseSettingsResponse | None:
    if service.kind != ServiceKind.DATABASE:
        return None
    config = service.database_config or {}
    return DatabaseSettingsResponse(
        image=config.get("image"),
        storage_gi=config.get("storageGi"),
        user=config.get("user"),
        database=config.get("database"),
    )


def _connection(service: Any, port: int) -> DatabaseConnectionResponse | None:
    if service.kind != ServiceKind.DATABASE or service.database_engine is None:
        return None
    engine = DatabaseEngine(service.database_engine)
    return DatabaseConnectionResponse(
        url_template=url_template(
            engine, internal_host(service.id), port, service.database_config or {}
        ),
        properties=get_engine_spec(engine).properties,
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
