"""분석기 gate CLI 계약(`iris.analysis-gate.v1`). 요청·응답 JSON 은 camelCase 다.

WAS 가 쓰는 필드만 검사하고 나머지는 그대로 둔다. 원문은 `repository_analyses.result` 에 남는다.
"""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.enums import AnalysisGateComplexity, AnalysisGateDecision, AnalysisGateMode, Builder

ANALYSIS_GATE_REQUEST_SCHEMA = "iris.analysis-gate-request.v1"
ANALYSIS_GATE_RESPONSE_SCHEMA = "iris.analysis-gate.v1"


class _WireModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")


class AnalysisGateRequest(BaseModel):
    """분석기에 넘기는 입력. source_root 는 Worker 가 푼 고정 커밋의 절대 경로다."""

    source_root: Path
    root_directory: str
    source_sha: str | None
    mode: AnalysisGateMode


class AnalysisGateSimpleBuild(_WireModel):
    builder: Builder
    dockerfile_path: str | None = None


class AnalysisGateBinding(_WireModel):
    """env 값이 다른 unit·의존성의 연결 정보에서 온다는 분석기의 추정."""

    kind: Literal["dependency", "unit"]
    target_id: str
    property: str
    # property=url 이고 코드가 쓴 URL 에서 읽은 값. 자격 증명(userinfo)은 분석기가 내지 않는다.
    scheme: str | None = None
    url_suffix: str = ""
    has_credentials: bool = False


class AnalysisGateEnv(_WireModel):
    key: str
    stage: str = "runtime"
    required: bool = False
    binding: AnalysisGateBinding | None = None


class AnalysisGateHostAlias(_WireModel):
    """코드가 호스트명으로 쓰는 다른 unit·의존성(`api:3000`)."""

    host: str
    port: int | None = None
    target_id: str


class AnalysisGateInitScript(_WireModel):
    """compose 가 `/docker-entrypoint-initdb.d` 에 넣는 초기화 스크립트. 내용은 분석기가 내지
    않는다.

    path 는 레포 루트 기준이다. sha256 은 아주 큰 파일이면 null(그때는 supported=false)이다.
    """

    path: str = Field(min_length=1, max_length=1024)
    kind: str
    sha256: str | None = None
    size: int = Field(ge=0)
    order: int = Field(ge=0)
    supported: bool = False


class AnalysisGateDependency(_WireModel):
    """compose 이미지 전용 서비스(DB 등). 비밀번호는 분석기가 내지 않는다."""

    id: str = Field(min_length=1, max_length=200)
    engine: str
    image: str | None = None
    port: int | None = None
    database: str | None = None
    user: str | None = None
    init_scripts: list[AnalysisGateInitScript] = Field(default_factory=list)


class AnalysisGateUnit(_WireModel):
    """배포 단위 하나. root_directory 는 레포 루트, dockerfile_path 는 그 unit 루트 기준이다."""

    id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    root_directory: str = "."
    builder: Builder | None = None
    dockerfile_path: str | None = None
    # compose `build.target`. 없으면 마지막 스테이지다.
    build_target: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    start_command: str | None = None
    build_command: str | None = None
    role: str | None = None
    env: list[AnalysisGateEnv] = Field(default_factory=list)
    host_aliases: list[AnalysisGateHostAlias] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)


class AnalysisGateResult(_WireModel):
    schema_version: Literal["iris.analysis-gate.v1"]
    source_sha: str | None = None
    decision: AnalysisGateDecision
    complexity: AnalysisGateComplexity
    simple_build: AnalysisGateSimpleBuild | None = None
    units: list[AnalysisGateUnit] = Field(default_factory=list)
    dependencies: list[AnalysisGateDependency] = Field(default_factory=list)
    # 분석 결과는 실행 승인이 아니다. 분석기가 다른 값을 주면 계약 위반이다.
    execution_authorized: Literal[False] = False

    def find_unit(self, unit_id: str) -> AnalysisGateUnit | None:
        return next((unit for unit in self.units if unit.id == unit_id), None)

    def find_dependency(self, dependency_id: str) -> AnalysisGateDependency | None:
        return next((d for d in self.dependencies if d.id == dependency_id), None)
