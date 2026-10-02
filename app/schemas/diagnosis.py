"""The v2 library result keeps its original nested keys and null values."""

from datetime import datetime

from pydantic import JsonValue

from app.enums import DiagnosisJobStatus
from app.models.deployment_diagnosis import DeploymentDiagnosis
from app.schemas.response import ApiModel


class DiagnosisResponse(ApiModel):
    id: str
    service_id: int
    deployment_id: int
    attempt_id: str
    status: DiagnosisJobStatus
    stage: str
    result: dict[str, JsonValue] | None = None
    error_code: str | None = None
    model_selection: dict[str, JsonValue] | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, row: DeploymentDiagnosis) -> "DiagnosisResponse":
        return cls.model_validate(row, from_attributes=True)
