"""Optional Build Worker analyzer configuration; no model/cloud credentials."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class BuildPreparationSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="BUILD_PREPARATION_", extra="ignore"
    )

    analyzer_command: list[str] = Field(default_factory=list)
    timeout_seconds: float = Field(default=120, gt=0, le=600)
