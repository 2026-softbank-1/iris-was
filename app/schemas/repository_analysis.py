from datetime import datetime
from typing import Annotated, Any

from pydantic import Field, StringConstraints

from app.enums import (
    AnalysisErrorCode,
    AnalysisGateComplexity,
    AnalysisGateDecision,
    AnalysisGateMode,
    Builder,
    RepositoryAnalysisStatus,
)
from app.models.repository_analysis import RepositoryAnalysis
from app.schemas.response import ApiModel
from app.schemas.service import (
    Branch,
    Command,
    DockerTarget,
    PathText,
    Port,
    ServiceName,
    ServiceResponse,
)
from app.schemas.stack import StackChangeResponse
from app.schemas.variable import VariablesValidationResponse
from app.services.repository_analysis_service import (
    AppliedAnalysis,
    DependencyProvisioning,
    UnitSelection,
)
from app.services.stack_apply_service import DependencySelection

UnitId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class CreateRepositoryAnalysisRequest(ApiModel):
    """서비스를 만들기 전에 레포 구성을 확인한다. 브랜치 최신 커밋을 접수 시점에 고정한다."""

    source_repository_url: Annotated[str, StringConstraints(min_length=1, max_length=500)] = Field(
        examples=["https://github.com/iris-org/shop"]
    )
    github_installation_id: int | None = Field(
        default=None,
        description=(
            "저장소에 접근하는 GitHub App 설치 id(`GET /api/v1/github/repos` 의 installationId)."
            " 생략하면 저장소 소유 계정의 설치를 쓴다. 다른 설치면 422."
        ),
    )
    source_branch: Branch | None = Field(
        default=None, description="생략하면 저장소 기본 브랜치다.", examples=["main"]
    )
    root_directory: PathText | None = Field(
        default=None, description="저장소 안의 분석 위치. 생략하면 루트다.", examples=["."]
    )
    mode: AnalysisGateMode = Field(
        default=AnalysisGateMode.AUTO,
        description="auto: 단순 레포면 분석 생략(skip). force: 단순해도 배포 단위를 분석한다.",
    )


class DependencyProvisioningResponse(ApiModel):
    engine: str = Field(examples=["mongodb"])
    image: str = Field(
        description="플랫폼이 띄우는 고정 공식 이미지(digest 고정). compose 이미지와 다를 수 있다.",
        examples=["docker.io/library/mongo:7@sha256:..."],
    )


class RepositoryAnalysisResponse(ApiModel):
    id: int
    project_id: int
    status: RepositoryAnalysisStatus = Field(
        description="QUEUED → RUNNING → SUCCEEDED | FAILED. apply 뒤에는 APPLIED."
    )
    decision: AnalysisGateDecision | None = Field(
        default=None, description="SUCCEEDED 뒤에만 있다. skip 이면 기존 단일 서비스 생성 경로."
    )
    complexity: AnalysisGateComplexity | None = None
    source_repository_url: str
    source_branch: str
    source_sha: str | None = Field(default=None, description="분석한(배포할) 고정 커밋.")
    root_directory: str | None = Field(default=None, description="없으면 저장소 루트다.")
    mode: AnalysisGateMode
    result: dict[str, Any] | None = Field(
        default=None,
        description=(
            "분석기 응답(`iris.analysis-gate.v1`) 원문. reasons·signals·simpleBuild·units·"
            "dependencies·questions 를 담는다."
        ),
    )
    error_code: AnalysisErrorCode | None = Field(default=None, description="FAILED 일 때 사유.")
    error_message: str | None = None
    applied_service_ids: list[int] | None = Field(
        default=None, description="apply 로 만든 서비스 id. APPLIED 일 때만 있다."
    )
    provisioning: dict[str, DependencyProvisioningResponse] | None = Field(
        default=None,
        description=(
            "`result.dependencies[].id` 마다 플랫폼이 개발용 DB 로 띄울 엔진·이미지(지원 엔진만)."
            " 분석 조회(GET)에만 있다."
        ),
    )
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(
        cls,
        analysis: RepositoryAnalysis,
        provisioning: dict[str, DependencyProvisioning] | None = None,
    ) -> "RepositoryAnalysisResponse":
        return cls(
            provisioning=(
                {
                    key: DependencyProvisioningResponse(engine=p.engine.value, image=p.image)
                    for key, p in provisioning.items()
                }
                or None
            )
            if provisioning is not None
            else None,
            id=analysis.id,
            project_id=analysis.project_id,
            status=analysis.status,
            decision=analysis.decision,
            complexity=analysis.complexity,
            source_repository_url=analysis.source_repository_url,
            source_branch=analysis.source_branch,
            source_sha=analysis.source_sha,
            root_directory=analysis.root_directory,
            mode=analysis.mode,
            result=analysis.result,
            error_code=analysis.error_code,
            error_message=analysis.error_message,
            applied_service_ids=analysis.applied_service_ids,
            created_at=analysis.created_at,
            updated_at=analysis.updated_at,
        )


class ApplyRepositoryAnalysisUnitRequest(ApiModel):
    """만들 배포 단위. 생략한 값은 분석 결과(`result.units[]`)의 값을 쓴다."""

    unit_id: UnitId = Field(description="`result.units[].id`", examples=["api"])
    name: ServiceName | None = Field(
        default=None, description="서비스 이름. 생략하면 unit 이름을 slug 로 바꿔 쓴다."
    )
    root_directory: PathText | None = Field(default=None, description="저장소 루트 기준.")
    builder: Builder | None = None
    dockerfile_path: PathText | None = Field(default=None, description="unit 루트 기준.")
    docker_target: DockerTarget | None = Field(
        default=None,
        description="Dockerfile `--target` 스테이지. 생략하면 분석의 `buildTarget` 을 쓴다.",
    )
    port: Port | None = None
    start_command: Command | None = None
    build_command: Command | None = None

    def to_selection(self) -> UnitSelection:
        return UnitSelection(
            unit_id=self.unit_id,
            name=self.name,
            root_directory=self.root_directory,
            builder=self.builder,
            dockerfile_path=self.dockerfile_path,
            docker_target=self.docker_target,
            port=self.port,
            start_command=self.start_command,
            build_command=self.build_command,
        )


class ApplyRepositoryAnalysisDependencyRequest(ApiModel):
    """분석된 의존성(`result.dependencies[]`)을 플랫폼이 개발용 DB 로 만들지."""

    dependency_id: UnitId = Field(description="`result.dependencies[].id`", examples=["postgres"])
    provision: bool = Field(default=True, description="false 면 만들지 않는다")
    name: ServiceName | None = Field(
        default=None, description="DB 서비스 이름. 생략하면 dependency id 를 slug 로 쓴다."
    )
    storage_gi: int | None = Field(default=None, ge=1, le=20, description="기본 5")

    def to_selection(self) -> DependencySelection:
        return DependencySelection(
            dependency_id=self.dependency_id,
            is_provisioned=self.provision,
            name=self.name,
            storage_gi=self.storage_gi,
        )


class ApplyRepositoryAnalysisRequest(ApiModel):
    units: list[ApplyRepositoryAnalysisUnitRequest] = Field(min_length=1, max_length=20)
    dependencies: list[ApplyRepositoryAnalysisDependencyRequest] | None = Field(
        default=None,
        max_length=20,
        description=(
            "생략하면 지원 엔진(postgres·mysql·mongodb·redis) 의존성을 모두 만든다(기능이 켜진 AWS"
            " 타깃일 때). 기능이 꺼졌거나 on-prem 이면 기본은 만들지 않고,"
            " provision=true 를 보내면 422."
        ),
    )
    skip_variable_validation: bool = Field(
        default=False, description="true 면 환경변수 error 가 있어도 배포를 접수한다"
    )
    deploy: bool = Field(
        default=True,
        description="true 면 만든 서비스마다 분석한 커밋으로 배포 요청(MANUAL)을 만든다.",
    )
    target_ids: list[int] | None = Field(
        default=None, description="배포 타깃 id. 정확히 1개다. 생략하면 `aws` 타깃이다."
    )
    is_auto_deploy: bool = Field(
        default=True,
        description="만드는 모든 서비스의 push 자동 배포 여부. 서비스 생성의 기본값과 같다.",
    )


class ServiceVariablesValidationResponse(VariablesValidationResponse):
    service_id: int


class GeneratedSecretResponse(ApiModel):
    id: str = Field(description="분석의 `result.secrets[].id`", examples=["MONGO_APP_PASSWORD"])
    service_ids: list[int] = Field(
        description="이번 apply 가 같은 값을 변수로 저장한 서비스(앱·DB). 값은 변수 탭에서 본다."
    )


class ApplyRepositoryAnalysisResponse(ApiModel):
    analysis_id: int
    services: list[ServiceResponse] = Field(description="앱 서비스(unit)")
    databases: list[ServiceResponse] = Field(
        default_factory=list, description="이 분석으로 만들었거나 이어 쓰는 관리형 DB 서비스"
    )
    stack_id: int | None = None
    stack_deployment_id: int | None = Field(
        default=None, description="의존 순서 배포를 접수했으면 그 스택 배포 id"
    )
    variable_issues: list[ServiceVariablesValidationResponse] | None = Field(
        default=None,
        description=(
            "환경변수 error 로 배포를 접수하지 않았으면 서비스별 검증 결과. 서비스는 만들어져"
            " 있으니 고친 뒤 스택 재배포(또는 같은 apply 재전송)를 한다."
        ),
    )

    changes: list[StackChangeResponse] | None = Field(
        default=None,
        description=(
            "증분 apply 에서 이미 있는 DB 의 초기화 스크립트가 분석과 달라졌으면 DEPENDENCY_CHANGED"
            "(reason init_scripts_changed). 이미 초기화된 DB 에는 다시 실행하지 않는다."
        ),
    )

    generated_secrets: list[GeneratedSecretResponse] | None = Field(
        default=None,
        description=(
            "분석이 찾은 `generate: random` 비밀값을 플랫폼이 만들어(이미 있으면 그 값을 나눠)"
            " consumer 에 저장한 것. 값은 응답에 없다. 처음 apply 한 호출에만 있다."
        ),
    )

    @classmethod
    def from_applied(cls, applied: AppliedAnalysis) -> "ApplyRepositoryAnalysisResponse":
        return cls(
            generated_secrets=[
                GeneratedSecretResponse(id=g.id, service_ids=g.service_ids)
                for g in applied.generated_secrets
            ]
            or None,
            analysis_id=applied.analysis_id,
            services=[ServiceResponse.from_detail(detail) for detail in applied.services],
            databases=[ServiceResponse.from_detail(detail) for detail in applied.databases],
            stack_id=applied.stack_id,
            stack_deployment_id=applied.stack_deployment_id,
            variable_issues=(
                [
                    ServiceVariablesValidationResponse(
                        service_id=v.service_id,
                        **VariablesValidationResponse.from_validation(v).model_dump(),
                    )
                    for v in applied.variable_validations
                ]
                or None
            ),
            changes=[StackChangeResponse.model_validate(c) for c in applied.changes] or None,
        )
