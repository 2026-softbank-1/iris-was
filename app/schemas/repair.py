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


class CreateAutomaticRepairRequest(ApiModel):
    model_config = CreateRepairRequest.model_config
    diagnosis_id: int | None = Field(default=None, gt=0)


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
    publication: dict[str, Any] | None = None
    auto_merge: bool = False
    auto_redeploy: bool = False

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
            publication=repair.request_metadata.get("publication"),
            auto_merge=bool(repair.request_metadata.get("autoMerge")),
            auto_redeploy=bool(repair.request_metadata.get("autoRedeploy")),
        )


class RepairAccessResponse(ApiModel):
    repository: str
    can_write: bool
    installation_url: str
    reason: str | None = None


class RepairGithubTokenRequest(ApiModel):
    repository: str = Field(
        min_length=3,
        max_length=201,
        pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$",
        description=(
            "서비스의 현재 source repository와 일치해야 하는 owner/repo. "
            "설치 ID·권한은 WAS가 결정한다."
        ),
        examples=["owner/repository"],
    )


class RepairGithubTokenResponse(ApiModel):
    repository: str = Field(
        description="발급한 토큰이 접근할 수 있는 서비스 소스 저장소 하나",
        examples=["owner/repository"],
    )
    token: str = Field(
        repr=False,
        description="Contents·Pull requests write 설치 토큰. 코디네이터 메모리에서만 사용한다.",
        examples=["<short-lived-installation-token>"],
    )
    expires_at: datetime = Field(
        description="GitHub가 정한 만료 시각(UTC). 코디네이터는 만료 60초 전에 재발급한다.",
        examples=["2030-01-01T01:00:00Z"],
    )
