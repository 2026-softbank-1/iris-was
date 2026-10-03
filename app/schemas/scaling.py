from app.schemas.response import ApiModel
from app.services.scaling_config import ResourceRequirements, ScalingConfig
from app.services.service_scaling_service import ServiceScalingDetail


class UpdateServiceScalingRequest(ScalingConfig):
    """Pod 수와 Pod 당 CPU·메모리 requests/limits 전체를 교체한다."""

    def to_config(self) -> ScalingConfig:
        return ScalingConfig.model_validate(self.model_dump(mode="json"))


class ServiceScalingResponse(ApiModel):
    service_id: int
    replicas: int
    resources: ResourceRequirements
    deployment_request_id: int | None = None

    @classmethod
    def from_detail(cls, detail: ServiceScalingDetail) -> "ServiceScalingResponse":
        return cls(
            service_id=detail.service_id,
            replicas=detail.scaling.replicas,
            resources=detail.scaling.resources,
            deployment_request_id=detail.deployment_request_id,
        )
