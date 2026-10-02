"""Operator-fixed diagnosis settings; credentials are never persisted with jobs."""

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, JsonValue, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class DiagnosisSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="DIAGNOSIS_", extra="ignore")

    provider: Literal["openai"] = "openai"
    model: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    api_key: SecretStr | None = None
    profile_id: str = Field(
        default="iris-deployment-diagnosis", pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
    )
    timeout_seconds: float = Field(default=60, gt=0, le=300)
    lease_seconds: int = Field(default=60, ge=15, le=600)
    poll_interval_seconds: float = Field(default=2, gt=0, le=60)
    max_output_tokens: int = Field(default=4096, ge=1024, le=16384)
    reasoning_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    max_evidence_bytes: int = Field(default=16384, ge=1024, le=65536)
    max_prompt_bytes: int = Field(default=32768, ge=16384, le=131072)
    # The analysis and diagnosis workers share this durable conservative ledger.
    budget_ledger: Path = Path("/tmp/iris-analysis-budget.json")
    max_cost_usd: float = Field(default=1, gt=0, le=100, allow_inf_nan=False)
    input_usd_per_million: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    output_usd_per_million: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    aws_region: str | None = None
    codebuild_project: str | None = None
    log_max_bytes: int = Field(default=10000, ge=1024, le=32768)
    log_max_pages: int = Field(default=5, ge=1, le=20)

    def model_selection(self) -> dict[str, JsonValue] | None:
        if not self.model or not self.api_key or not self.api_key.get_secret_value().strip():
            return None
        return {
            "provider": self.provider,
            "model": self.model,
            "profileId": self.profile_id,
            "timeoutSeconds": self.timeout_seconds,
            "maxOutputTokens": self.max_output_tokens,
            "reasoningEffort": self.reasoning_effort,
            "maxEvidenceBytes": self.max_evidence_bytes,
            "maxPromptBytes": self.max_prompt_bytes,
            "inputUsdPerMillion": self.input_usd_per_million,
            "outputUsdPerMillion": self.output_usd_per_million,
            "maxCostUsd": self.max_cost_usd,
        }

    def settings_version(self) -> str:
        return hashlib.sha256(
            json.dumps(self.model_selection(), sort_keys=True).encode()
        ).hexdigest()


@lru_cache
def get_diagnosis_settings() -> DiagnosisSettings:
    return DiagnosisSettings()
