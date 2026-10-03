from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import DeployWorkerSettings
from app.enums import DeploymentStatus, DeploymentStrategy, DeploymentTrigger, TargetKind
from app.services.builder_detection import DeployConfig
from app.services.deploy_service import DEADLINE_MARGIN, DeployService, _deadline
from app.services.deployment_strategy import resolve_deployment_strategy, strategy_extra_wait
from app.services.scaling_config import ScalingConfig
from tests.fakes_deployment import HEAD_SHA, OWNER, DeploymentSetup
from tests.test_service_scaling_service import deployed_request, scaling_service


def _scaling(replicas: int) -> dict[str, Any]:
    config = ScalingConfig.defaults().model_dump(mode="json")
    config["replicas"] = replicas
    return config


@pytest.fixture
async def setup() -> DeploymentSetup:
    setup = await DeploymentSetup().build()
    setup.deployment_strategy_enabled = True
    return setup


AWS = TargetKind.AWS
ONPREM = TargetKind.ONPREM


@pytest.mark.parametrize(
    ("requested", "replicas", "target_kind", "is_enabled", "expected"),
    [
        (DeploymentStrategy.ROLLING, 3, AWS, True, DeploymentStrategy.ROLLING),
        (DeploymentStrategy.CANARY, 2, AWS, True, DeploymentStrategy.CANARY),
        (DeploymentStrategy.BLUE_GREEN, 10, AWS, True, DeploymentStrategy.BLUE_GREEN),
        (DeploymentStrategy.CANARY, 1, AWS, True, DeploymentStrategy.ROLLING),
        (DeploymentStrategy.BLUE_GREEN, 0, AWS, True, DeploymentStrategy.ROLLING),
        (DeploymentStrategy.CANARY, 3, AWS, False, DeploymentStrategy.ROLLING),
        (DeploymentStrategy.ROLLING, 0, AWS, False, DeploymentStrategy.ROLLING),
        (DeploymentStrategy.CANARY, 3, ONPREM, True, DeploymentStrategy.ROLLING),
        (DeploymentStrategy.BLUE_GREEN, 3, ONPREM, True, DeploymentStrategy.ROLLING),
        (DeploymentStrategy.ROLLING, 3, ONPREM, True, DeploymentStrategy.ROLLING),
    ],
)
def test_resolve_deployment_strategy_table_returns_applied_strategy(
    requested: DeploymentStrategy,
    replicas: int,
    target_kind: TargetKind,
    is_enabled: bool,
    expected: DeploymentStrategy,
) -> None:
    applied = resolve_deployment_strategy(
        requested, replicas, target_kind=target_kind, is_enabled=is_enabled
    )

    assert applied == expected


@pytest.mark.parametrize(
    ("strategy", "expected"),
    [
        (None, timedelta()),
        (DeploymentStrategy.ROLLING, timedelta()),
        (DeploymentStrategy.CANARY, timedelta(seconds=60)),
        (DeploymentStrategy.BLUE_GREEN, timedelta(seconds=60)),
    ],
)
def test_strategy_extra_wait_by_strategy_returns_fixed_wait(
    strategy: DeploymentStrategy | None, expected: timedelta
) -> None:
    assert strategy_extra_wait(strategy) == expected


@pytest.mark.parametrize(
    ("strategy", "extra_seconds"),
    [(None, 0), (DeploymentStrategy.ROLLING, 0), (DeploymentStrategy.CANARY, 60)],
)
def test_deadline_with_strategy_adds_fixed_wait(
    strategy: DeploymentStrategy | None, extra_seconds: int
) -> None:
    before = datetime.now(UTC)

    deadline = _deadline(DeployConfig(healthcheck_timeout=120), strategy)

    base = timedelta(seconds=120 + extra_seconds) + DEADLINE_MARGIN
    assert before + base <= deadline <= datetime.now(UTC) + base


def _deploy_service(*, is_enabled: bool) -> DeployService:
    settings = DeployWorkerSettings.model_construct(deployment_strategy_enabled=is_enabled)
    return DeployService(None, None, None, None, settings, "w")  # type: ignore[arg-type]


def _release(strategy: DeploymentStrategy | None, target_kind: TargetKind = AWS) -> Any:
    return SimpleNamespace(
        deployment_request=SimpleNamespace(deployment_strategy=strategy),
        target=SimpleNamespace(kind=target_kind),
    )


@pytest.mark.parametrize(
    ("strategy", "expected"),
    [
        (DeploymentStrategy.CANARY, DeploymentStrategy.CANARY),
        (DeploymentStrategy.BLUE_GREEN, DeploymentStrategy.BLUE_GREEN),
        # 기능 도입 전에 만든 요청은 방식이 없다.
        (None, DeploymentStrategy.ROLLING),
    ],
)
def test_deploy_service_strategy_with_flag_on_writes_applied_strategy(
    strategy: DeploymentStrategy | None, expected: DeploymentStrategy
) -> None:
    service = _deploy_service(is_enabled=True)

    assert service._deployment_strategy(_release(strategy)) == expected


def test_deploy_service_strategy_with_flag_off_omits_values_key() -> None:
    service = _deploy_service(is_enabled=False)

    assert service._deployment_strategy(_release(DeploymentStrategy.CANARY)) is None


@pytest.mark.parametrize("strategy", [None, *DeploymentStrategy])
def test_deploy_service_strategy_for_onprem_target_omits_values_key(
    strategy: DeploymentStrategy | None,
) -> None:
    # on-prem 은 chart 0.6.0 에 남아 있어 schema 가 deploymentStrategy 키를 거절한다.
    service = _deploy_service(is_enabled=True)

    assert service._deployment_strategy(_release(strategy, ONPREM)) is None


@pytest.mark.parametrize("trigger_type", [DeploymentTrigger.PUSH, DeploymentTrigger.CLI])
async def test_create_deployment_request_records_requested_and_applied_strategy(
    setup: DeploymentSetup, trigger_type: DeploymentTrigger
) -> None:
    setup.service.deployment_strategy = DeploymentStrategy.CANARY
    setup.service.scaling_config = _scaling(3)

    request = await setup.deployment_request_service().create_deployment_request(
        setup.service,
        source_sha=HEAD_SHA,
        source_commit_message=None,
        trigger_type=trigger_type,
        idempotency_key=f"strategy-{trigger_type}",
    )

    assert request is not None
    assert request.requested_deployment_strategy == DeploymentStrategy.CANARY
    assert request.deployment_strategy == DeploymentStrategy.CANARY


async def test_manual_deployment_without_strategy_records_rolling(setup: DeploymentSetup) -> None:
    request = await setup.manual_service().create_deployment_request(
        OWNER, setup.service.id, trigger_type=DeploymentTrigger.MANUAL
    )

    assert request.requested_deployment_strategy == DeploymentStrategy.ROLLING
    assert request.deployment_strategy == DeploymentStrategy.ROLLING


@pytest.mark.parametrize("replicas", [0, 1])
async def test_manual_deployment_with_too_few_replicas_falls_back_to_rolling(
    setup: DeploymentSetup, replicas: int
) -> None:
    setup.service.deployment_strategy = DeploymentStrategy.BLUE_GREEN
    setup.service.scaling_config = _scaling(replicas)

    request = await setup.manual_service().create_deployment_request(
        OWNER, setup.service.id, trigger_type=DeploymentTrigger.MANUAL
    )

    assert request.requested_deployment_strategy == DeploymentStrategy.BLUE_GREEN
    assert request.deployment_strategy == DeploymentStrategy.ROLLING


async def test_manual_deployment_with_flag_off_applies_rolling(setup: DeploymentSetup) -> None:
    setup.deployment_strategy_enabled = False
    setup.service.deployment_strategy = DeploymentStrategy.CANARY
    setup.service.scaling_config = _scaling(3)

    request = await setup.manual_service().create_deployment_request(
        OWNER, setup.service.id, trigger_type=DeploymentTrigger.MANUAL
    )

    assert request.requested_deployment_strategy == DeploymentStrategy.CANARY
    assert request.deployment_strategy == DeploymentStrategy.ROLLING


@pytest.mark.parametrize(
    ("trigger_type", "replicas", "expected"),
    [
        (DeploymentTrigger.REDEPLOY, 2, DeploymentStrategy.CANARY),
        (DeploymentTrigger.ROLLBACK, 2, DeploymentStrategy.CANARY),
        (DeploymentTrigger.RESTART, 2, DeploymentStrategy.CANARY),
        (DeploymentTrigger.ROLLBACK, 1, DeploymentStrategy.ROLLING),
        (DeploymentTrigger.RESTART, 1, DeploymentStrategy.ROLLING),
    ],
)
async def test_reused_source_deployment_follows_current_strategy_rule(
    setup: DeploymentSetup,
    trigger_type: DeploymentTrigger,
    replicas: int,
    expected: DeploymentStrategy,
) -> None:
    source = await deployed_request(setup)
    setup.service.deployment_strategy = DeploymentStrategy.CANARY
    setup.service.scaling_config = _scaling(replicas)

    request = await setup.manual_service().create_deployment_request(
        OWNER,
        setup.service.id,
        trigger_type=trigger_type,
        source_deployment_request_id=(
            None if trigger_type == DeploymentTrigger.RESTART else source.id
        ),
    )

    assert request.requested_deployment_strategy == DeploymentStrategy.CANARY
    assert request.deployment_strategy == expected
    assert source.deployment_strategy == DeploymentStrategy.ROLLING


async def test_scaling_restart_below_two_replicas_falls_back_to_rolling(
    setup: DeploymentSetup,
) -> None:
    await deployed_request(setup)
    setup.service.deployment_strategy = DeploymentStrategy.CANARY
    setup.service.scaling_config = _scaling(2)

    detail = await scaling_service(setup).update_scaling(
        OWNER, setup.service.id, ScalingConfig.model_validate(_scaling(1))
    )

    request = setup.requests.requests[-1]
    assert detail.deployment_request_id == request.id
    assert request.trigger_type == DeploymentTrigger.RESTART
    assert request.requested_deployment_strategy == DeploymentStrategy.CANARY
    assert request.deployment_strategy == DeploymentStrategy.ROLLING


async def test_remove_request_leaves_both_strategies_empty(setup: DeploymentSetup) -> None:
    await deployed_request(setup)
    setup.service.deployment_strategy = DeploymentStrategy.CANARY
    setup.service.scaling_config = _scaling(3)

    request = await setup.manual_service().create_deployment_request(
        OWNER, setup.service.id, trigger_type=DeploymentTrigger.REMOVE
    )

    assert request.trigger_type == DeploymentTrigger.REMOVE
    assert request.status == DeploymentStatus.DEPLOYING
    assert request.requested_deployment_strategy is None
    assert request.deployment_strategy is None


async def test_manual_deployment_on_onprem_target_applies_rolling(setup: DeploymentSetup) -> None:
    setup.services.targets[setup.service.id] = {2}  # onprem
    setup.service.deployment_strategy = DeploymentStrategy.CANARY
    setup.service.scaling_config = _scaling(3)

    request = await setup.manual_service().create_deployment_request(
        OWNER, setup.service.id, trigger_type=DeploymentTrigger.MANUAL
    )

    assert request.requested_deployment_strategy == DeploymentStrategy.CANARY
    assert request.deployment_strategy == DeploymentStrategy.ROLLING
