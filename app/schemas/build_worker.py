from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.enums import Builder


def validate_relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or any(ord(character) < 32 for character in value)
        or value.startswith("-")
        or (value != "." and path.as_posix() != value)
    ):
        raise ValueError("a normalized relative source path is required")
    return value


class BuildConfig(BaseModel):
    """Confirmed analyzer plan copied into the queue; workers never guess a builder."""

    model_config = ConfigDict(extra="forbid")

    builder: Builder
    dockerfile_path: str | None = None
    platform: str = Field(default="linux/amd64", pattern=r"^linux/(amd64|arm64)$")
    port: int = Field(ge=1, le=65535)
    build_command: str | None = Field(default=None, max_length=10000)
    start_command: str | None = Field(default=None, max_length=10000)
    railpack_version: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{1,32}$")
    root_directory: str = "."
    runtime_env: list[dict[str, Any]] = Field(default_factory=list)
    analysis_plan_digest: str | None = Field(default=None, max_length=128)
    pipeline_run_id: str | None = Field(default=None, max_length=36)
    healthcheck_path: str | None = Field(default=None, pattern=r"^/[^\r\n]*$", max_length=255)
    healthcheck_timeout: int = Field(default=300, ge=1, le=3600)

    @field_validator("dockerfile_path")
    @classmethod
    def validate_dockerfile_path(cls, value: str | None) -> str | None:
        return validate_relative_path(value) if value is not None else None

    @field_validator("root_directory")
    @classmethod
    def validate_root_directory(cls, value: str) -> str:
        return validate_relative_path(value)

    @model_validator(mode="after")
    def validate_builder_configuration(self) -> "BuildConfig":
        if self.builder == Builder.DOCKERFILE and self.dockerfile_path is None:
            raise ValueError("the confirmed Dockerfile path is required")
        return self


class BuildJobPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    source_repository_url: str
    source_branch: str
    source_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    root_directory: str = "."
    github_installation_id: int | None = Field(default=None, ge=1)
    target_ids: list[int] | None = None
    build_config: BuildConfig

    @field_validator("root_directory", mode="before")
    @classmethod
    def normalize_root_directory(cls, value: Any) -> str:
        return validate_relative_path(value or ".")
