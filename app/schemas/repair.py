from datetime import datetime
from typing import Any, Literal, Self

from pydantic import ConfigDict, Field, field_validator

from app.models.deployment_repair import DeploymentRepair
from app.schemas.response import ApiModel


class CreateRepairRequest(ApiModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        alias_generator=ApiModel.model_config["alias_generator"],
    )
    diagnosis_id: int = Field(gt=0, description="Successful diagnosis belonging to this deployment")
    plan_ids: list[str] = Field(
        min_length=1, max_length=20, description="Selected original remediation plan IDs"
    )

    @field_validator("plan_ids")
    @classmethod
    def validate_plan_ids(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values) or any(
            not value or len(value) > 128 for value in values
        ):
            raise ValueError("Plan IDs must be nonempty and unique")
        return values


class RepairResponse(ApiModel):
    id: int
    deployment_id: int
    diagnosis_id: int
    status: Literal["RUNNING", "SUCCEEDED", "FAILED", "UNKNOWN_OUTCOME"]
    source_sha: str
    plan_ids: list[str]
    result: dict[str, Any] | None = None
    error_code: str | None = None
    created_at: datetime
    finished_at: datetime | None = None

    @classmethod
    def from_model(cls, repair: DeploymentRepair) -> Self:
        return cls(
            id=repair.id,
            deployment_id=repair.deployment_request_id,
            diagnosis_id=repair.diagnosis_id,
            status=repair.status,
            source_sha=repair.source_sha,
            plan_ids=repair.plan_ids,
            result=repair.result,
            error_code=repair.error_code,
            created_at=repair.created_at,
            finished_at=repair.finished_at,
        )


class RepairGithubTokenRequest(ApiModel):
    repository: str = Field(
        min_length=3, max_length=201, pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
    )


class RepairGithubTokenResponse(ApiModel):
    repository: str
    token: str = Field(
        repr=False, description="코디네이터 전용 단기 토큰. 저장·로그·모델 입력 금지"
    )
    expires_at: datetime
