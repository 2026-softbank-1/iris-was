"""Typed, fixed-source plan handed from the analyzer to platform build workers."""

import hashlib
import json
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from pydantic.alias_generators import to_camel

from app.core.exceptions import InvalidInputError


class PipelineBuildConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    builder: Literal["dockerfile", "railpack"]
    dockerfile_path: str | None = None
    platform: Literal["linux/amd64", "linux/arm64"]
    root_directory: str
    port: int | None = Field(default=None, ge=1, le=65535)
    build_command: str | None = Field(default=None, max_length=2000)
    start_command: str | None = Field(default=None, max_length=2000)
    railpack_version: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]+$", max_length=32)
    runtime_env: list[dict[str, Any]] = Field(default_factory=list)
    pipeline_run_id: str
    analysis_plan_digest: str | None = None

    @field_validator("root_directory", "dockerfile_path")
    @classmethod
    def validate_path(cls, value: str | None) -> str | None:
        if value is None:
            return value
        path = PurePosixPath(value)
        if (
            not value
            or path.is_absolute()
            or ".." in path.parts
            or "\\" in value
            or any(ord(char) < 32 for char in value)
        ):
            raise ValueError("build paths must stay inside the source repository")
        return path.as_posix()

    @model_validator(mode="after")
    def validate_builder(self) -> "PipelineBuildConfig":
        if self.builder == "dockerfile" and (
            not self.dockerfile_path or self.dockerfile_path == "."
        ):
            raise ValueError("Dockerfile builds require a selected file")
        if self.builder == "railpack" and (
            self.dockerfile_path is not None or not self.railpack_version
        ):
            raise ValueError("Railpack builds require a fixed version and no Dockerfile path")
        return self


class PipelinePlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, alias_generator=to_camel)
    schema_version: Literal["iris.pipeline-plan.v1"]
    pipeline_run_id: str
    service_id: int
    source_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    source_repository_url: str
    source_snapshot_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    analysis_result_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    selected_service_candidate_id: str
    build_config: PipelineBuildConfig
    target_ids: list[int] = Field(min_length=1)
    target_bindings: list[dict[str, Any]] = Field(min_length=1)
    analyzer_plan_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    user_inputs_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    authorization: Literal["user_requested_pipeline"]
    execution_authorized: Literal[False] = False


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def validate_pipeline_plan(plan: dict[str, Any]) -> None:
    try:
        PipelinePlan.model_validate(plan)
    except ValidationError:
        raise InvalidInputError("pipeline plan does not match the worker contract") from None


def digest_pipeline_plan(plan: dict[str, Any]) -> str:
    return digest(plan)
