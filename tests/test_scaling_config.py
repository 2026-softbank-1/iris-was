from copy import deepcopy
from typing import Any

import pytest
from pydantic import ValidationError

from app.services.scaling_config import ScalingConfig

DEFAULTS: dict[str, Any] = {
    "replicas": 1,
    "resources": {
        "requests": {"cpu": "100m", "memory": "256Mi"},
        "limits": {"cpu": "1", "memory": "512Mi"},
    },
}


def test_defaults_match_infra_chart() -> None:
    assert ScalingConfig.defaults().model_dump(mode="json") == DEFAULTS


@pytest.mark.parametrize("replicas", [0, 1, 10])
def test_replica_count_supports_zero_and_inclusive_limit(replicas: int) -> None:
    config = ScalingConfig.model_validate(DEFAULTS | {"replicas": replicas})

    assert config.replicas == replicas


@pytest.mark.parametrize("replicas", [-1, 11, 1.0, "1", True, False, None])
def test_replica_count_rejects_out_of_range_and_coerced_values(replicas: object) -> None:
    with pytest.raises(ValidationError):
        ScalingConfig.model_validate(DEFAULTS | {"replicas": replicas})


@pytest.mark.parametrize(
    ("requests", "limits"),
    [
        ({"cpu": "250m", "memory": "512Mi"}, {"cpu": "0.5", "memory": "1Gi"}),
        ({"cpu": "0.125", "memory": "0.5Gi"}, {"cpu": "125m", "memory": "512Mi"}),
        ({"cpu": "1m", "memory": "500000000"}, {"cpu": "2", "memory": "512M"}),
        ({"cpu": "100m", "memory": "1Gi"}, {"cpu": "1", "memory": "1.1G"}),
    ],
)
def test_resources_compare_equivalent_and_mixed_kubernetes_units(
    requests: dict[str, str], limits: dict[str, str]
) -> None:
    config = ScalingConfig.model_validate(
        {"replicas": 2, "resources": {"requests": requests, "limits": limits}}
    )

    assert config.model_dump(mode="json")["resources"] == {
        "requests": requests,
        "limits": limits,
    }


@pytest.mark.parametrize(
    ("resource", "quantity"),
    [
        ("cpu", "0"),
        ("cpu", "-100m"),
        ("cpu", "1.5m"),
        ("cpu", "0.0001"),
        ("cpu", "NaN"),
        ("cpu", "1Gi"),
        ("cpu", " 100m"),
        ("cpu", 1),
        ("memory", "0Mi"),
        ("memory", "-1Gi"),
        ("memory", "256MB"),
        ("memory", "256mi"),
        ("memory", "NaN"),
        ("memory", 256),
    ],
)
def test_resources_reject_invalid_or_nonpositive_quantity_strings(
    resource: str, quantity: object
) -> None:
    body = deepcopy(DEFAULTS)
    body["resources"]["requests"][resource] = quantity

    with pytest.raises(ValidationError):
        ScalingConfig.model_validate(body)


@pytest.mark.parametrize(
    ("resource", "requested_quantity", "limit"),
    [("cpu", "1001m", "1"), ("memory", "1Gi", "1000M")],
)
def test_requests_cannot_exceed_limits(resource: str, requested_quantity: str, limit: str) -> None:
    body = deepcopy(DEFAULTS)
    body["resources"]["requests"][resource] = requested_quantity
    body["resources"]["limits"][resource] = limit

    with pytest.raises(ValidationError):
        ScalingConfig.model_validate(body)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"replicas": 1},
        {"resources": DEFAULTS["resources"]},
        DEFAULTS | {"resources": {"requests": DEFAULTS["resources"]["requests"]}},
        DEFAULTS | {"replicaCount": 1},
        DEFAULTS
        | {
            "resources": DEFAULTS["resources"]
            | {"requests": DEFAULTS["resources"]["requests"] | {"gpu": "1"}}
        },
        DEFAULTS | {"resources": DEFAULTS["resources"] | {"extra": {}}},
    ],
)
def test_config_requires_complete_shape_and_forbids_extra_fields(body: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ScalingConfig.model_validate(body)
