"""빌더 결정 규칙. 우선순위: 코드 설정(iris.json) > 서비스 설정 > 자동 감지."""

import posixpath
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel

from app.core.exceptions import BuildFailedError
from app.enums import Builder, FailureCode
from app.models import Service

CONFIG_FILE_NAME = "iris.json"
DEFAULT_DOCKERFILE_PATH = "Dockerfile"


class _IrisBuildConfig(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, extra="ignore")

    builder: Builder | None = None
    dockerfile_path: str | None = None
    build_command: str | None = None


class IrisConfig(BaseModel):
    """서비스 레포의 {root_directory}/iris.json. deploy.* 는 Deploy Worker 가 읽게 넘긴다."""

    model_config = ConfigDict(extra="ignore")

    build: _IrisBuildConfig = Field(default_factory=_IrisBuildConfig)
    deploy: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class BuildPlan:
    builder: Builder
    dockerfile_path: str
    railpack_env: dict[str, str] = field(default_factory=dict)
    deploy_config: dict[str, Any] = field(default_factory=dict)


def parse_iris_config(content: bytes) -> IrisConfig:
    try:
        return IrisConfig.model_validate_json(content)
    except ValidationError as exc:
        raise BuildFailedError(
            FailureCode.BUILD_CONFIG_REQUIRED, f"invalid {CONFIG_FILE_NAME}"
        ) from exc


def detect_builder(file_names: set[str], config: IrisConfig | None, service: Service) -> BuildPlan:
    """file_names 는 root_directory 기준 상대 경로다."""
    config = config or IrisConfig()
    builder = config.build.builder or service.builder
    dockerfile_path = posixpath.normpath(
        config.build.dockerfile_path or service.dockerfile_path or DEFAULT_DOCKERFILE_PATH
    )
    has_dockerfile = dockerfile_path in file_names
    if builder == Builder.AUTO:
        builder = Builder.DOCKERFILE if has_dockerfile else Builder.RAILPACK
    if builder == Builder.DOCKERFILE and not has_dockerfile:
        raise BuildFailedError(
            FailureCode.BUILD_CONFIG_REQUIRED,
            "dockerfile not found",
            dockerfile_path=dockerfile_path,
        )

    railpack_env: dict[str, str] = {}
    if builder == Builder.RAILPACK:
        start_command = config.deploy.get("startCommand")
        if config.build.build_command:
            railpack_env["RAILPACK_BUILD_CMD"] = config.build.build_command
        if isinstance(start_command, str) and start_command:
            railpack_env["RAILPACK_START_CMD"] = start_command
    return BuildPlan(builder, dockerfile_path, railpack_env, config.deploy)
