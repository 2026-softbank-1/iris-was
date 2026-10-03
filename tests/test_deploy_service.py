import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.clients.argocd_client import ArgoAppStatus
from app.enums import Builder, DeploymentStrategy
from app.services.builder_detection import DeployConfig
from app.services.deploy_service import (
    Verdict,
    evaluate_release,
    render_service_values,
    service_host_label,
)
from app.services.scaling_config import ScalingConfig

NOW = datetime(2026, 10, 1, tzinfo=UTC)
DEADLINE = NOW + timedelta(minutes=5)


def _status(
    sync_status: str = "Synced",
    health_status: str = "Healthy",
    operation_phase: str = "Succeeded",
) -> ArgoAppStatus:
    return ArgoAppStatus(sync_status, "rev", health_status, operation_phase, "rev", None)


@pytest.mark.parametrize(
    ("status", "sync_contained", "operation_contained", "now", "expected"),
    [
        (None, False, False, NOW, Verdict.WAIT),
        (None, False, False, DEADLINE + timedelta(seconds=1), Verdict.TIMED_OUT),
        (_status(), False, False, NOW, Verdict.WAIT),
        (_status(operation_phase="Failed"), False, True, NOW, Verdict.FAILED),
        (_status(operation_phase="Error"), True, True, NOW, Verdict.FAILED),
        (_status(), True, False, NOW, Verdict.SUCCEEDED),
        (_status(health_status="Degraded"), True, False, NOW, Verdict.FAILED),
        # revert 직후: 목표 커밋은 봤지만 아직 적용 전이라 이전 release 의 Degraded 가 남아 있다.
        (
            _status(sync_status="OutOfSync", health_status="Degraded"),
            True,
            False,
            NOW,
            Verdict.WAIT,
        ),
        (_status(health_status="Progressing"), True, False, NOW, Verdict.WAIT),
        # 카나리·블루그린 Rollout 이 단계 사이에서 멈춘 동안이다.
        (_status(health_status="Suspended"), True, False, NOW, Verdict.WAIT),
        (
            _status(health_status="Suspended"),
            True,
            False,
            DEADLINE + timedelta(seconds=1),
            Verdict.TIMED_OUT,
        ),
        (
            _status(health_status="Progressing"),
            True,
            False,
            DEADLINE + timedelta(seconds=1),
            Verdict.TIMED_OUT,
        ),
    ],
)
def test_evaluate_release_table_returns_verdict(
    status: ArgoAppStatus | None,
    sync_contained: bool,
    operation_contained: bool,
    now: datetime,
    expected: Verdict,
) -> None:
    assert evaluate_release(status, sync_contained, operation_contained, now, DEADLINE) == expected


def test_evaluate_release_previous_failed_operation_waits() -> None:
    # 직전 release 의 Failed operation 이 남아 있고, Argo 가 아직 새 커밋을 못 봤다.
    status = _status(sync_status="Synced", health_status="Degraded", operation_phase="Failed")

    assert evaluate_release(status, False, False, NOW, DEADLINE) == Verdict.WAIT


def _render(
    deploy: DeployConfig,
    builder: Builder = Builder.DOCKERFILE,
    *,
    scaling: ScalingConfig | None = None,
    iris: dict[str, Any] | None = None,
    variables: dict[str, Any] | None = None,
    deployment_strategy: DeploymentStrategy | None = None,
) -> dict[str, Any]:
    content = render_service_values(
        host_label="my-app",
        release_id=345,
        image_repository="123.dkr.ecr.ap-northeast-2.amazonaws.com/iris/services/12",
        image_digest="sha256:abc",
        source_sha="f" * 40,
        builder=builder,
        deploy=deploy,
        base_domain="example.app",
        iris=iris,
        variables=variables,
        scaling=scaling,
        deployment_strategy=deployment_strategy,
    )
    values: dict[str, Any] = json.loads(content)
    return values


def test_render_service_values_default_has_deploy_specific_fields_only() -> None:
    assert _render(DeployConfig()) == {
        "image": {
            "repository": "123.dkr.ecr.ap-northeast-2.amazonaws.com/iris/services/12",
            "digest": "sha256:abc",
        },
        "release": {"id": 345, "sourceSha": "f" * 40},
        "containerPort": 8080,
        "health": {"timeoutSeconds": 300},
        "route": {"host": "my-app.example.app"},
    }


@pytest.mark.parametrize(
    "source_sha",
    [
        "upload-55f60ed8e30b",  # CLI 업로드
        "abcdef1",  # 짧은 해시
        "F" * 40,  # 대문자는 chart 스키마가 거절한다
        "f" * 41,
        "",
    ],
)
def test_render_service_values_omits_source_sha_the_chart_schema_would_reject(
    source_sha: str,
) -> None:
    # Argo CD 가 values 검증에서 실패하면 release 가 PENDING 에서 멈춘다. 필드를 생략해 막는다.
    content = render_service_values(
        host_label="my-app",
        release_id=345,
        image_repository="123.dkr.ecr.ap-northeast-2.amazonaws.com/iris/services/12",
        image_digest="sha256:abc",
        source_sha=source_sha,
        builder=Builder.DOCKERFILE,
        deploy=DeployConfig(),
        base_domain="example.app",
    )

    assert json.loads(content)["release"] == {"id": 345}


def test_render_service_values_keeps_full_lowercase_git_sha() -> None:
    assert _render(DeployConfig())["release"] == {"id": 345, "sourceSha": "f" * 40}


def test_render_service_values_iris_is_written_as_given() -> None:
    iris = {"serviceName": "my-app", "targetName": "aws", "deploymentId": 6789012}

    assert _render(DeployConfig(), iris=iris)["iris"] == iris


def test_render_service_values_without_iris_omits_the_key() -> None:
    assert "iris" not in _render(DeployConfig())


def test_render_service_values_variables_are_written_as_given() -> None:
    sealed = {"name": "vars-r345", "encryptedData": {"DATABASE_URL": "AgBy3i4TQXw="}}

    values = _render(DeployConfig(), variables=sealed)

    assert values["variables"] == sealed
    assert "plain" not in json.dumps(values)


def test_render_service_values_without_variables_omits_the_key() -> None:
    assert "variables" not in _render(DeployConfig())


@pytest.mark.parametrize("strategy", list(DeploymentStrategy))
def test_render_service_values_with_strategy_writes_deployment_strategy(
    strategy: DeploymentStrategy,
) -> None:
    assert _render(DeployConfig(), deployment_strategy=strategy)["deploymentStrategy"] == (
        strategy.value
    )


def test_render_service_values_without_strategy_omits_the_key() -> None:
    assert "deploymentStrategy" not in _render(DeployConfig())


def test_render_service_values_healthcheck_path_sets_health_path() -> None:
    values = _render(DeployConfig(healthcheck_path="/health", healthcheck_timeout=120))

    assert values["health"] == {"path": "/health", "timeoutSeconds": 120}


@pytest.mark.parametrize("replicas", [0, 3, 10])
def test_render_service_values_applies_pod_count_and_resources(replicas: int) -> None:
    scaling = ScalingConfig.model_validate(
        {
            "replicas": replicas,
            "resources": {
                "requests": {"cpu": "500m", "memory": "512Mi"},
                "limits": {"cpu": "2", "memory": "1Gi"},
            },
        }
    )

    values = _render(DeployConfig(healthcheck_path="/ready"), scaling=scaling)

    assert values["replicas"] == replicas
    assert values["resources"] == scaling.model_dump(mode="json")["resources"]
    assert values["health"] == {"path": "/ready", "timeoutSeconds": 300}
    assert values["image"]["digest"] == "sha256:abc"


@pytest.mark.parametrize(
    ("builder", "start_command", "expected"),
    [
        (
            Builder.DOCKERFILE,
            "node dist/main.js --port 8080",
            ["node", "dist/main.js", "--port", "8080"],
        ),
        (Builder.DOCKERFILE, "sh -c 'exec node main.js'", ["sh", "-c", "exec node main.js"]),
        (Builder.DOCKERFILE, None, None),
        # Railpack 은 빌드 때 이미지에 넣었으므로 덮어쓰지 않는다.
        (Builder.RAILPACK, "node dist/main.js", None),
    ],
)
def test_render_service_values_start_command_overrides_dockerfile_only(
    builder: Builder, start_command: str | None, expected: list[str] | None
) -> None:
    values = _render(DeployConfig(start_command=start_command), builder)

    assert values.get("command") == expected


@pytest.mark.parametrize(
    ("name", "service_id", "expected"),
    [
        ("web", 12, "web-12"),
        ("My App", 12, "my-app-12"),
        ("web_api.v2", 7, "web-api-v2-7"),
        ("내 서비스", 3, "service-3"),
        ("---", 3, "service-3"),
    ],
)
def test_service_host_label_makes_dns_label_unique_by_id(
    name: str, service_id: int, expected: str
) -> None:
    assert service_host_label(name, service_id) == expected


@pytest.mark.parametrize("service_id", [1, 12, 123456])
def test_service_host_label_long_name_stays_within_dns_label_limit(service_id: int) -> None:
    label = service_host_label("a" * 63, service_id)

    assert len(label) <= 63
    assert label.endswith(f"-{service_id}")
    assert re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", label)


def test_service_host_label_truncation_never_leaves_trailing_hyphen() -> None:
    name = "a" * 58 + "-b"

    label = service_host_label(name, 12)

    assert "--" not in label
    assert re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", label)
