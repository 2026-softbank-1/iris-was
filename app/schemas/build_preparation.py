"""Worker-side source preparation contracts, before CodeBuild submission."""

from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from app.enums import Builder


def validate_source_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError("source paths must stay inside the service root")
    return path.as_posix()


class PrepareBuildRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_directory: Path
    output_directory: Path
    source_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    root_directory: str = "."
    builder: Builder | None
    dockerfile_path: str | None = None
    platform: Literal["linux/amd64", "linux/arm64"] = "linux/amd64"

    @field_validator("source_directory", "output_directory")
    @classmethod
    def validate_absolute_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("worker directories must be absolute")
        return value

    @field_validator("root_directory")
    @classmethod
    def validate_root_directory(cls, value: str) -> str:
        return validate_source_path(value)

    @field_validator("dockerfile_path")
    @classmethod
    def validate_dockerfile_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = validate_source_path(value)
        if value == ".":
            raise ValueError("Dockerfile must be a file path")
        return value


class BuildEvidenceSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_source_path(value)


class BuildSourceArchiveSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    format: Literal["tar.gz"]


class BuildHandoffSchema(BaseModel):
    """Builder advice is separate from the service owner's recorded selection."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    owner: Literal["service"]
    requested_builder: Builder | None = Field(alias="requestedBuilder")
    recommended_builder: Builder = Field(alias="recommendedBuilder")
    decision_required: bool = Field(alias="decisionRequired")
    reason_code: Literal[
        "source_dockerfile",
        "dockerfile_absent",
        "explicit_railpack",
        "explicit_dockerfile_missing",
        "dockerfile_selection_required",
    ] = Field(alias="reasonCode")


class PrepareBuildResponse(BaseModel):
    """Analyzer wire names are converted once into WAS domain field names."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_version: Literal["iris.build-preparation.v2"] = Field(alias="schemaVersion")
    status: Literal["ready", "needs_input"]
    builder: Builder
    build_handoff: BuildHandoffSchema = Field(alias="buildHandoff")
    root_directory: str = Field(alias="rootDirectory")
    platform: Literal["linux/amd64", "linux/arm64"]
    source_sha: str = Field(alias="sourceSha", pattern=r"^[a-f0-9]{40}$")
    dockerfile_path: str | None = Field(alias="dockerfilePath")
    dockerfile_origin: Literal["source"] | None = Field(alias="dockerfileOrigin")
    dockerfile_sha256: str | None = Field(alias="dockerfileSha256", pattern=r"^[a-f0-9]{64}$")
    template_id: None = Field(alias="templateId")
    source_manifest_sha256: str | None = Field(
        alias="sourceManifestSha256", pattern=r"^[a-f0-9]{64}$"
    )
    source_archive: BuildSourceArchiveSchema | None = Field(alias="sourceArchive")
    analysis_source_snapshot_id: str | None = Field(alias="analysisSourceSnapshotId", default=None)
    analysis_context_hash: str | None = Field(alias="analysisContextHash", default=None)
    plan_digest: str | None = Field(alias="planDigest", default=None)
    preparation_digest: str | None = Field(alias="preparationDigest", default=None)
    analysis_mode: Literal["static", "opencode"] | None = Field(alias="analysisMode", default=None)
    analysis_result: dict[str, JsonValue] | None = Field(alias="analysisResult", default=None)
    source_readiness: dict[str, JsonValue] | None = Field(alias="sourceReadiness", default=None)
    evidence: list[BuildEvidenceSchema] = Field(default_factory=list, max_length=20000)
    unresolved_inputs: list[str] = Field(alias="unresolvedInputs", default_factory=list)
    execution_authorized: Literal[False] = Field(alias="executionAuthorized", default=False)

    @field_validator("root_directory")
    @classmethod
    def validate_root_directory(cls, value: str) -> str:
        return validate_source_path(value)

    @field_validator("dockerfile_path")
    @classmethod
    def validate_dockerfile_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = validate_source_path(value)
        if value == ".":
            raise ValueError("Dockerfile must be a file path")
        return value
