import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.clients.argocd_client import ArgoAppStatus
from app.enums import Builder
from app.services.builder_detection import DeployConfig
from app.services.deploy_service import Verdict, evaluate_release, render_service_values

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


def _render(deploy: DeployConfig, builder: Builder = Builder.DOCKERFILE) -> dict[str, Any]:
    content = render_service_values(
        slug="my-app",
        release_id=345,
        image_repository="123.dkr.ecr.ap-northeast-2.amazonaws.com/iris/services/12",
        image_digest="sha256:abc",
        source_sha="f" * 40,
        builder=builder,
        deploy=deploy,
        base_domain="example.app",
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


def test_render_service_values_healthcheck_path_sets_health_path() -> None:
    values = _render(DeployConfig(healthcheck_path="/health", healthcheck_timeout=120))

    assert values["health"] == {"path": "/health", "timeoutSeconds": 120}


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
