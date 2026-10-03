from typing import Any

import pytest

from app.core.exceptions import (
    DeploymentInProgressError,
    InvalidInputError,
    NoSucceededDeploymentError,
    ServiceNotFoundError,
)
from app.enums import Builder, BuildStatus, DeploymentStatus, DeploymentTrigger, JobKind
from app.models.deployment_request import DeploymentRequest
from app.services.scaling_config import ScalingConfig
from app.services.service_scaling_service import ServiceScalingService
from tests.fakes_deployment import OWNER, DeploymentSetup
from tests.test_scaling_config import DEFAULTS

SCALED: dict[str, Any] = {
    "replicas": 3,
    "resources": {
        "requests": {"cpu": "500m", "memory": "512Mi"},
        "limits": {"cpu": "2", "memory": "1Gi"},
    },
}
IMAGE_DIGEST = "sha256:" + "a" * 64


def scaling_service(setup: DeploymentSetup) -> ServiceScalingService:
    return ServiceScalingService(
        setup.session,  # type: ignore[arg-type]
        setup.services,  # type: ignore[arg-type]
        setup.requests,  # type: ignore[arg-type]
        setup.builds,  # type: ignore[arg-type]
        setup.deployment_request_service(),
    )


async def deployed_request(setup: DeploymentSetup) -> DeploymentRequest:
    request = await setup.manual_service().create_deployment_request(
        OWNER, setup.service.id, trigger_type=DeploymentTrigger.MANUAL
    )
    request.status = DeploymentStatus.SUCCEEDED
    build = setup.builds.builds[-1]
    build.builder = Builder.RAILPACK
    build.source_sha = request.source_sha
    build.image_repository = "123.dkr.ecr.ap-northeast-2.amazonaws.com/iris/services/1"
    build.image_tag = f"b-{build.id}"
    build.deploy_config = {"healthcheck_timeout": 60}
    build.succeed(IMAGE_DIGEST)
    return request


@pytest.fixture
async def setup() -> DeploymentSetup:
    return await DeploymentSetup().build()


async def test_get_returns_infra_defaults_for_unconfigured_service(setup: DeploymentSetup) -> None:
    detail = await scaling_service(setup).get_scaling(OWNER, setup.service.id)

    assert detail.service_id == setup.service.id
    assert detail.scaling.model_dump(mode="json") == DEFAULTS
    assert setup.session.commit_count == 0


async def test_get_returns_saved_desired_config(setup: DeploymentSetup) -> None:
    setup.service.scaling_config = SCALED

    detail = await scaling_service(setup).get_scaling(OWNER, setup.service.id)

    assert detail.scaling.model_dump(mode="json") == SCALED


@pytest.mark.parametrize("operation", ["get", "update"])
async def test_scaling_hides_other_users_service(setup: DeploymentSetup, operation: str) -> None:
    service = scaling_service(setup)

    with pytest.raises(ServiceNotFoundError):
        if operation == "get":
            await service.get_scaling(OWNER + 1, setup.service.id)
        else:
            await service.update_scaling(
                OWNER + 1, setup.service.id, ScalingConfig.model_validate(SCALED)
            )

    assert setup.service.scaling_config is None
    assert setup.requests.requests == []


async def test_update_snapshots_config_and_reuses_latest_image_without_build(
    setup: DeploymentSetup,
) -> None:
    live = await deployed_request(setup)
    source_build = setup.builds.builds[0]
    jobs_before = len(setup.jobs.jobs)
    commits_before = setup.session.commit_count

    detail = await scaling_service(setup).update_scaling(
        OWNER, setup.service.id, ScalingConfig.model_validate(SCALED), idempotency_key="scale-1"
    )

    request = setup.requests.requests[-1]
    copied = setup.builds.builds[-1]
    assert detail.service_id == setup.service.id
    assert detail.deployment_request_id == request.id
    assert detail.scaling.model_dump(mode="json") == SCALED
    assert setup.service.scaling_config == SCALED
    assert request.scaling_snapshot == SCALED
    assert request.idempotency_key == f"scaling:{setup.service.id}:scale-1"
    assert (request.trigger_type, request.status, request.requested_by) == (
        DeploymentTrigger.RESTART,
        DeploymentStatus.DEPLOYING,
        OWNER,
    )
    assert request.source_deployment_request_id == live.id
    assert (request.source_sha, request.source_commit_message) == (
        live.source_sha,
        live.source_commit_message,
    )
    assert copied.status == BuildStatus.SUCCEEDED
    assert (copied.image_repository, copied.image_digest, copied.deploy_config) == (
        source_build.image_repository,
        source_build.image_digest,
        source_build.deploy_config,
    )
    assert [(job.kind, job.payload) for job in setup.jobs.jobs[jobs_before:]] == [
        (JobKind.DEPLOY, {"build_id": copied.id})
    ]
    assert setup.session.commit_count == commits_before + 1


async def test_update_without_successful_deployment_leaves_desired_config_unchanged(
    setup: DeploymentSetup,
) -> None:
    with pytest.raises(NoSucceededDeploymentError):
        await scaling_service(setup).update_scaling(
            OWNER, setup.service.id, ScalingConfig.model_validate(SCALED)
        )

    assert setup.service.scaling_config is None
    assert setup.requests.requests == []
    assert setup.session.commit_count == 0


@pytest.mark.parametrize("missing", ["build", "image_repository", "image_digest", "success"])
async def test_update_rejects_successful_deployment_without_usable_image(
    setup: DeploymentSetup, missing: str
) -> None:
    await deployed_request(setup)
    if missing == "build":
        setup.builds.builds.clear()
    elif missing == "success":
        setup.builds.builds[0].status = BuildStatus.FAILED
    else:
        setattr(setup.builds.builds[0], missing, None)

    with pytest.raises(InvalidInputError):
        await scaling_service(setup).update_scaling(
            OWNER, setup.service.id, ScalingConfig.model_validate(SCALED)
        )

    assert setup.service.scaling_config is None
    assert len(setup.requests.requests) == 1


async def test_update_after_successful_removal_rejects_absent_live_service(
    setup: DeploymentSetup,
) -> None:
    await deployed_request(setup)
    removed = await setup.manual_service().create_deployment_request(
        OWNER, setup.service.id, trigger_type=DeploymentTrigger.REMOVE
    )
    removed.status = DeploymentStatus.SUCCEEDED

    with pytest.raises(NoSucceededDeploymentError):
        await scaling_service(setup).update_scaling(
            OWNER, setup.service.id, ScalingConfig.model_validate(SCALED)
        )

    assert setup.service.scaling_config is None
    assert len(setup.requests.requests) == 2


@pytest.mark.parametrize("same_config", [False, True])
async def test_update_while_active_rejects_even_unchanged_config(
    setup: DeploymentSetup, same_config: bool
) -> None:
    await deployed_request(setup)
    await setup.manual_service().create_deployment_request(
        OWNER, setup.service.id, trigger_type=DeploymentTrigger.RESTART
    )
    config = ScalingConfig.defaults() if same_config else ScalingConfig.model_validate(SCALED)
    commits_before = setup.session.commit_count

    with pytest.raises(DeploymentInProgressError):
        await scaling_service(setup).update_scaling(OWNER, setup.service.id, config)

    assert setup.service.scaling_config is None
    assert len(setup.requests.requests) == 2
    assert setup.session.commit_count == commits_before


async def test_same_idempotency_key_replays_initial_config_and_request(
    setup: DeploymentSetup,
) -> None:
    await deployed_request(setup)
    service = scaling_service(setup)
    config = ScalingConfig.model_validate(SCALED)
    first = await service.update_scaling(OWNER, setup.service.id, config, idempotency_key="s-1")
    commits_before = setup.session.commit_count

    replayed = await service.update_scaling(OWNER, setup.service.id, config, idempotency_key="s-1")

    assert replayed.deployment_request_id == first.deployment_request_id
    assert replayed.scaling == first.scaling
    assert len(setup.requests.requests) == 2
    assert setup.session.commit_count == commits_before


async def test_same_idempotency_key_with_changed_body_rejects_request(
    setup: DeploymentSetup,
) -> None:
    await deployed_request(setup)
    service = scaling_service(setup)
    await service.update_scaling(
        OWNER, setup.service.id, ScalingConfig.model_validate(SCALED), idempotency_key="s-1"
    )

    with pytest.raises(InvalidInputError):
        await service.update_scaling(
            OWNER, setup.service.id, ScalingConfig.defaults(), idempotency_key="s-1"
        )

    assert setup.service.scaling_config == SCALED
    assert len(setup.requests.requests) == 2


async def test_old_idempotency_replay_does_not_revert_new_desired_config(
    setup: DeploymentSetup,
) -> None:
    await deployed_request(setup)
    service = scaling_service(setup)
    first = await service.update_scaling(
        OWNER, setup.service.id, ScalingConfig.model_validate(SCALED), idempotency_key="s-1"
    )
    setup.requests.requests[-1].status = DeploymentStatus.SUCCEEDED
    newer_config = ScalingConfig.model_validate(SCALED | {"replicas": 4})
    await service.update_scaling(OWNER, setup.service.id, newer_config, idempotency_key="s-2")
    commits_before = setup.session.commit_count

    replayed = await service.update_scaling(
        OWNER, setup.service.id, ScalingConfig.model_validate(SCALED), idempotency_key="s-1"
    )

    assert replayed.deployment_request_id == first.deployment_request_id
    assert replayed.scaling.model_dump(mode="json") == SCALED
    assert setup.service.scaling_config == newer_config.model_dump(mode="json")
    assert len(setup.requests.requests) == 3
    assert setup.session.commit_count == commits_before


async def test_unchanged_applied_config_returns_live_request_without_new_deployment(
    setup: DeploymentSetup,
) -> None:
    live = await deployed_request(setup)
    jobs_before = len(setup.jobs.jobs)
    commits_before = setup.session.commit_count

    detail = await scaling_service(setup).update_scaling(
        OWNER, setup.service.id, ScalingConfig.defaults()
    )

    assert detail.deployment_request_id == live.id
    assert len(setup.requests.requests) == 1
    assert len(setup.jobs.jobs) == jobs_before
    assert setup.session.commit_count == commits_before


async def test_failed_scaling_can_retry_same_desired_config(setup: DeploymentSetup) -> None:
    live = await deployed_request(setup)
    service = scaling_service(setup)
    config = ScalingConfig.model_validate(SCALED)
    first = await service.update_scaling(OWNER, setup.service.id, config)
    setup.requests.requests[-1].status = DeploymentStatus.FAILED

    retry = await service.update_scaling(OWNER, setup.service.id, config)

    assert retry.deployment_request_id != first.deployment_request_id
    assert setup.requests.requests[-1].source_deployment_request_id == live.id
    assert len(setup.requests.requests) == 3


async def test_request_creation_failure_does_not_persist_new_desired_config(
    setup: DeploymentSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    await deployed_request(setup)
    commits_before = setup.session.commit_count

    async def fail_save(*args: object, **kwargs: object) -> None:
        raise RuntimeError("job storage unavailable")

    monkeypatch.setattr(setup.jobs, "save", fail_save)

    with pytest.raises(RuntimeError, match="job storage unavailable"):
        await scaling_service(setup).update_scaling(
            OWNER, setup.service.id, ScalingConfig.model_validate(SCALED)
        )

    assert setup.service.scaling_config is None
    assert setup.session.commit_count == commits_before
