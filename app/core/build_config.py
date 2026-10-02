from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class BuildWorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    github_app_id: str
    github_app_private_key: SecretStr
    github_api_base_url: str = "https://api.github.com"
    aws_region: str
    codebuild_project: str
    artifact_bucket: str
    concurrency: int = Field(default=2, ge=1, le=16)
    lease_seconds: int = Field(default=300, ge=30)
    poll_interval_seconds: float = Field(default=5.0, gt=0)
    build_timeout_minutes: int = Field(default=15, ge=5, le=480)
    railpack_version: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{1,32}$")
    railpack_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


@lru_cache
def get_build_worker_settings() -> BuildWorkerSettings:
    return BuildWorkerSettings()
