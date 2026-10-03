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


class AnalysisGateUnit(_WireModel):
    """배포 단위 하나. root_directory 는 레포 루트, dockerfile_path 는 그 unit 루트 기준이다."""

    id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    root_directory: str = "."
    builder: Builder | None = None
    dockerfile_path: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    start_command: str | None = None
    build_command: str | None = None
    role: str | None = None


class AnalysisGateResult(_WireModel):
    schema_version: Literal["iris.analysis-gate.v1"]
    source_sha: str | None = None
    decision: AnalysisGateDecision
    complexity: AnalysisGateComplexity
    simple_build: AnalysisGateSimpleBuild | None = None
    units: list[AnalysisGateUnit] = Field(default_factory=list)
    # 분석 결과는 실행 승인이 아니다. 분석기가 다른 값을 주면 계약 위반이다.
    execution_authorized: Literal[False] = False

    def find_unit(self, unit_id: str) -> AnalysisGateUnit | None:
        return next((unit for unit in self.units if unit.id == unit_id), None)
