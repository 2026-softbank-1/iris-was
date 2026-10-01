"""빌더 결정 규칙. 우선순위: 코드 설정(iris.json) > 서비스 설정 > 자동 감지."""

import posixpath
import shlex
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
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


class DeployConfig(BaseModel):
    """iris.json 의 deploy.*. 배포 단계엔 돌려줄 실패 코드가 없어 빌드 전에 검증한다.

    모르는 키는 거절한다. 아직 지원하지 않는 preDeployCommand 를 조용히 무시하면
    마이그레이션이 돌지 않은 채 배포된다.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    healthcheck_path: str | None = Field(default=None, pattern=r"^/\S*$", max_length=1024)
    # progressDeadlineSeconds 로 쓴다. 너무 짧으면 이미지 pull 만으로 넘겨 정상 앱이 실패한다.
    healthcheck_timeout: int = Field(default=300, ge=30, le=3600)
    start_command: str | None = None

    @field_validator("start_command")
    @classmethod
    def _check_start_command(cls, value: str | None) -> str | None:
        # Dockerfile 빌드는 배포 때 shlex 로 나눠 command 로 쓴다. 따옴표가 깨지면 미리 거절한다.
        if value is not None and not shlex.split(value):
            raise ValueError("startCommand is empty")
        return value

    @property
    def start_command_args(self) -> list[str] | None:
        return shlex.split(self.start_command) if self.start_command else None


class IrisConfig(BaseModel):
    """서비스 레포의 {root_directory}/iris.json. deploy.* 는 Deploy Worker 가 읽게 넘긴다."""

    model_config = ConfigDict(extra="ignore")

    build: _IrisBuildConfig = Field(default_factory=_IrisBuildConfig)
    deploy: DeployConfig = Field(default_factory=DeployConfig)


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
        if config.build.build_command:
            railpack_env["RAILPACK_BUILD_CMD"] = config.build.build_command
        if config.deploy.start_command:
            railpack_env["RAILPACK_START_CMD"] = config.deploy.start_command
    deploy_config = config.deploy.model_dump(by_alias=True, exclude_unset=True)
    return BuildPlan(builder, dockerfile_path, railpack_env, deploy_config)
