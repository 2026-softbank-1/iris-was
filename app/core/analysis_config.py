"""Analysis Worker configuration; model credentials never enter jobs or API responses."""

import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, JsonValue, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class AnalysisSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="ANALYSIS_", extra="ignore")

    provider: str | None = None
    model: str | None = None
    api_key: SecretStr | None = None
    server_url: str | None = None
    server_password: SecretStr | None = None
    executable: str = "opencode"
    output_mode: Literal["json_text", "structured"] = "json_text"
    timeout_seconds: float = Field(default=180, gt=0, le=600)
    lease_seconds: int = Field(default=60, ge=15, le=600)
    poll_interval_seconds: float = Field(default=2, gt=0, le=60)
    budget_ledger: Path = Path("/tmp/iris-analysis-budget.json")
    max_cost_usd: float = Field(default=1, gt=0, le=100)

    def model_config_values(self) -> dict[str, JsonValue] | None:
        has_key = self.api_key is not None and bool(self.api_key.get_secret_value().strip())
        if not self.provider or not self.model or (not has_key and not self.server_url):
            return None
        values: dict[str, JsonValue] = {
            "provider": self.provider,
            "model": self.model,
            "timeout_seconds": self.timeout_seconds,
            "output_mode": self.output_mode,
        }
        for name in ("server_url", "api_key", "server_password"):
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                values[name] = value.get_secret_value()
            elif isinstance(value, str) and value:
                values[name] = value
        return values

    def model_selection(self) -> dict[str, JsonValue] | None:
        if self.model_config_values() is None:
            return None
        return {
            "provider": self.provider,
            "model": self.model,
            "outputMode": self.output_mode,
            "timeoutSeconds": self.timeout_seconds,
            "serverIdentity": hashlib.sha256((self.server_url or "isolated").encode()).hexdigest(),
            "maxCostUsd": self.max_cost_usd,
        }


@lru_cache
def get_analysis_settings() -> AnalysisSettings:
    return AnalysisSettings()
