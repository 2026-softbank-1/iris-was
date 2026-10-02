from datetime import datetime
from typing import Literal

from pydantic import ConfigDict, Field, JsonValue, model_validator

from app.enums import Builder, PipelineStatus
from app.models.pipeline_run import PipelineRun
from app.schemas.response import ApiModel
from app.schemas.service import Command, Port


class StartPipelineRequest(ApiModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["opencode", "static"] = "opencode"
    auto_deploy: bool = True
    enable_auto_deploy: bool = True

    @model_validator(mode="after")
    def respect_plan_only_request(self) -> "StartPipelineRequest":
        if not self.auto_deploy and "enable_auto_deploy" not in self.model_fields_set:
            self.enable_auto_deploy = False
        return self


class PipelineVariable(ApiModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
    value: str | None = Field(default=None, max_length=2048)
    secret_ref: str | None = Field(
        default=None, pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", max_length=253
    )
    secret_key: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]+$", max_length=253)

    @model_validator(mode="after")
    def validate_binding(self) -> "PipelineVariable":
        if self.key in {"IRIS_PUBLIC_DOMAIN", "IRIS_GIT_COMMIT_SHA"}:
            raise ValueError("platform runtime variables cannot be overridden")
        if (self.value is None) == (self.secret_ref is None) or bool(self.secret_ref) != bool(
            self.secret_key
        ):
            raise ValueError("provide a public value or a Secret reference and key")
        if self.value is not None and any(
            part in self.key.upper()
            for part in (
                "PASSWORD",
                "SECRET",
                "TOKEN",
                "API_KEY",
                "PRIVATE_KEY",
                "DATABASE_URL",
            )
        ):
            raise ValueError("sensitive variables require an existing Secret reference")
        return self


class PipelineAnswers(ApiModel):
    model_config = ConfigDict(extra="forbid")
    service_candidate_id: str | None = Field(default=None, max_length=255)
    builder: Builder | None = None
    dockerfile_path: str | None = Field(default=None, max_length=255)
    port: Port | None = None
    build_command: Command | None = None
    start_command: Command | None = None
    variables: list[PipelineVariable] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def unique_variables(self) -> "PipelineAnswers":
        if len({item.key for item in self.variables}) != len(self.variables):
            raise ValueError("variable names must be unique")
        return self


class PipelineResponse(ApiModel):
    id: str
    service_id: int
    status: PipelineStatus
    stage: str
    mode: str
    auto_deploy: bool
    enable_auto_deploy: bool
    source_sha: str
    analysis_id: str | None = None
    deployment_request_id: int | None = None
    plan_digest: str | None = None
    execution_plan: dict[str, JsonValue] | None = None
    deployment_dossier: dict[str, JsonValue] | None = None
    planning_report: dict[str, JsonValue] | None = None
    questions: list[dict[str, JsonValue]]
    error_code: str | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, run: PipelineRun) -> "PipelineResponse":
        return cls.model_validate(run, from_attributes=True)
