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
from app.schemas.service import Branch, Command, PathText, Port, ServiceName, ServiceResponse
from app.services.repository_analysis_service import AppliedAnalysis, UnitSelection

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
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, analysis: RepositoryAnalysis) -> "RepositoryAnalysisResponse":
        return cls(
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
            port=self.port,
            start_command=self.start_command,
            build_command=self.build_command,
        )


class ApplyRepositoryAnalysisRequest(ApiModel):
    units: list[ApplyRepositoryAnalysisUnitRequest] = Field(min_length=1, max_length=20)
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


class ApplyRepositoryAnalysisResponse(ApiModel):
    analysis_id: int
    services: list[ServiceResponse]

    @classmethod
    def from_applied(cls, applied: AppliedAnalysis) -> "ApplyRepositoryAnalysisResponse":
        return cls(
            analysis_id=applied.analysis_id,
            services=[ServiceResponse.from_detail(detail) for detail in applied.services],
        )
