from decimal import Decimal
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

CpuQuantity = Annotated[
    str,
    StringConstraints(
        max_length=32,
        pattern=r"^(?:[0-9]+(?:\.[0-9]{1,3})?|\.[0-9]{1,3}|[0-9]+m)$",
    ),
]
MemoryQuantity = Annotated[
    str,
    StringConstraints(
        max_length=32,
        pattern=r"^[0-9]+(?:\.[0-9]{1,3})?(?:[KMGTPE]i|[kMGTPE])?$",
    ),
]

_MEMORY_FACTORS: dict[str, int] = {
    "": 1,
    **{suffix: 1024**power for power, suffix in enumerate(("Ki", "Mi", "Gi", "Ti", "Pi", "Ei"), 1)},
    **{suffix: 1000**power for power, suffix in enumerate(("k", "M", "G", "T", "P", "E"), 1)},
}
_MAX_QUANTITY = Decimal(2**63 - 1)


def _cpu_cores(value: str) -> Decimal:
    return Decimal(value[:-1]) / 1000 if value.endswith("m") else Decimal(value)


def _memory_bytes(value: str) -> Decimal:
    suffix = value[-2:] if value.endswith("i") else value[-1:] if value[-1].isalpha() else ""
    amount = value[: -len(suffix)] if suffix else value
    return Decimal(amount) * _MEMORY_FACTORS[suffix]


class ResourceQuantity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cpu: CpuQuantity
    memory: MemoryQuantity

    @field_validator("cpu")
    @classmethod
    def validate_cpu(cls, value: str) -> str:
        if not 0 < _cpu_cores(value) <= _MAX_QUANTITY:
            raise ValueError("cpu must be positive and fit a Kubernetes quantity")
        return value

    @field_validator("memory")
    @classmethod
    def validate_memory(cls, value: str) -> str:
        if not 0 < _memory_bytes(value) <= _MAX_QUANTITY:
            raise ValueError("memory must be positive and fit a Kubernetes quantity")
        return value


class ResourceRequirements(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requests: ResourceQuantity
    limits: ResourceQuantity

    @model_validator(mode="after")
    def validate_requests_fit_limits(self) -> Self:
        if _cpu_cores(self.requests.cpu) > _cpu_cores(self.limits.cpu):
            raise ValueError("resources.requests.cpu must not exceed resources.limits.cpu")
        if _memory_bytes(self.requests.memory) > _memory_bytes(self.limits.memory):
            raise ValueError("resources.requests.memory must not exceed resources.limits.memory")
        return self


class ScalingConfig(BaseModel):
    """iris-service chart 의 replicas 와 Pod 별 resources 계약."""

    model_config = ConfigDict(extra="forbid")

    replicas: Annotated[int, Field(strict=True, ge=0, le=10)]
    resources: ResourceRequirements

    @classmethod
    def defaults(cls) -> Self:
        return cls(
            replicas=1,
            resources=ResourceRequirements(
                requests=ResourceQuantity(cpu="100m", memory="256Mi"),
                limits=ResourceQuantity(cpu="1", memory="512Mi"),
            ),
        )
