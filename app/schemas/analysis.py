"""Service analysis API; nested analyzer contracts retain their original keys and nulls."""

from datetime import datetime
from pathlib import PurePosixPath
from typing import Literal

from pydantic import ConfigDict, Field, JsonValue, field_validator, model_validator

from app.enums import AnalysisJobStatus, Builder
from app.models.service_analysis import ServiceAnalysis
from app.schemas.response import ApiModel
from app.schemas.service import Command, Port


class CreateAnalysisRequest(ApiModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["opencode", "static"] = "opencode"


class CancelAnalysisRequest(ApiModel):
    model_config = ConfigDict(extra="forbid")
    analysis_id: str = Field(min_length=1, max_length=36)


class ConfirmAnalysisRequest(CancelAnalysisRequest):
    service_candidate_id: str = Field(min_length=1, max_length=255)
    builder: Builder
    dockerfile_path: str | None = Field(default=None, max_length=255)
    port: Port | None = None
    build_command: Command | None = None
    start_command: Command | None = None

    @field_validator("dockerfile_path")
    @classmethod
    def validate_dockerfile_path(cls, value: str | None) -> str | None:
        if value is None:
            return value
        path = PurePosixPath(value)
        if (
            not value
            or value == "."
            or path.is_absolute()
            or ".." in path.parts
            or "\\" in value
            or any(ord(char) < 32 for char in value)
        ):
            raise ValueError("Dockerfile path must stay within the selected service root")
        return path.as_posix()

    @model_validator(mode="after")
    def validate_builder_selection(self) -> "ConfirmAnalysisRequest":
        if self.builder == Builder.RAILPACK and self.dockerfile_path is not None:
            raise ValueError("Railpack selection cannot specify a Dockerfile path")
        return self


class AnalysisResponse(ApiModel):
    id: str
    service_id: int
    status: AnalysisJobStatus
    mode: Literal["opencode", "static"]
    stage: str
    source_sha: str
    source_repository_url: str
    source_branch: str
    root_directory: str
    source_snapshot_id: str | None = None
    context_hash: str | None = None
    result_digest: str | None = None
    analysis_status: str | None = None
    analysis_result: dict[str, JsonValue] | None = None
    verification_report: dict[str, JsonValue] | None = None
    source_readiness: dict[str, JsonValue] | None = None
    deployment_dossier: dict[str, JsonValue] | None = None
    run_report: dict[str, JsonValue] | None = None
    model_selection: dict[str, JsonValue] | None = None
    evidence: list[dict[str, JsonValue]] | None = None
    builder_recommendation: str | None = None
    review_required: bool
    deployment_authorized: Literal[False] = False
    error_code: str | None = None
    selected_service_candidate_id: str | None = None
    confirmed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, analysis: ServiceAnalysis) -> "AnalysisResponse":
        return cls.model_validate(analysis, from_attributes=True)
