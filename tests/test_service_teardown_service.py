import pytest

from app.core.exceptions import DeploymentInProgressError
from app.enums import DeploymentStatus, DeploymentTrigger, JobKind
from app.models.deployment_request import DeploymentRequest
from app.models.service import Service
from app.services.service_teardown_service import ServiceTeardownService
from tests.fakes_deployment import OWNER, DeploymentSetup


@pytest.fixture
async def setup() -> DeploymentSetup:
    return await DeploymentSetup().build()


def _teardown(setup: DeploymentSetup) -> ServiceTeardownService:
    return ServiceTeardownService(
        setup.requests,  # type: ignore[arg-type]
        setup.deployment_request_service(),
    )


async def _deploy(
    setup: DeploymentSetup, service: Service, status: DeploymentStatus
) -> DeploymentRequest:
    request = await setup.manual_service().create_deployment_request(
        OWNER, service.id, trigger_type=DeploymentTrigger.MANUAL
    )
    request.status = status
    return request


async def _other_service(setup: DeploymentSetup, name: str) -> Service:
    return await setup.services.save(
        Service(
            project_id=setup.service.project_id,
            name=name,
            source_repository_url=setup.service.source_repository_url,
            github_installation_id=setup.service.github_installation_id,
            source_branch="main",
            is_auto_deploy=False,
        )
    )


async def test_request_teardown_never_deployed_service_creates_nothing(
    setup: DeploymentSetup,
) -> None:
    created = await _teardown(setup).request_teardown([setup.service], OWNER)

    assert created == 0
    assert setup.requests.requests == []


async def test_request_teardown_live_service_creates_remove_request_and_job(
    setup: DeploymentSetup,
) -> None:
    live = await _deploy(setup, setup.service, DeploymentStatus.SUCCEEDED)

    created = await _teardown(setup).request_teardown([setup.service], OWNER)

    request = setup.requests.requests[-1]
    assert created == 1
    assert (request.trigger_type, request.status) == (
        DeploymentTrigger.REMOVE,
        DeploymentStatus.DEPLOYING,
    )
    assert (request.source_deployment_request_id, request.requested_by) == (live.id, OWNER)
    assert setup.jobs.jobs[-1].kind == JobKind.REMOVE
    assert request.idempotency_key.startswith(f"delete:{setup.service.id}:")


async def test_request_teardown_failed_first_deploy_still_cleans_up(
    setup: DeploymentSetup,
) -> None:
    # 첫 배포가 실패해도 GitOps 디렉터리와 Application 이 남을 수 있다.
    failed = await _deploy(setup, setup.service, DeploymentStatus.FAILED)

    created = await _teardown(setup).request_teardown([setup.service], OWNER)

    assert created == 1
    assert setup.requests.requests[-1].source_deployment_request_id == failed.id


async def test_request_teardown_already_removed_service_creates_nothing(
    setup: DeploymentSetup,
) -> None:
    await _deploy(setup, setup.service, DeploymentStatus.SUCCEEDED)
    teardown = _teardown(setup)
    await teardown.request_teardown([setup.service], OWNER)
    setup.requests.requests[-1].status = DeploymentStatus.SUCCEEDED
    requests_before = len(setup.requests.requests)

    created = await teardown.request_teardown([setup.service], OWNER)

    assert created == 0
    assert len(setup.requests.requests) == requests_before


async def test_request_teardown_after_blocked_remove_requests_again(
    setup: DeploymentSetup,
) -> None:
    await _deploy(setup, setup.service, DeploymentStatus.SUCCEEDED)
    teardown = _teardown(setup)
    await teardown.request_teardown([setup.service], OWNER)
    setup.requests.requests[-1].status = DeploymentStatus.MANUAL_INTERVENTION

    created = await teardown.request_teardown([setup.service], OWNER)

    assert created == 1
    assert setup.requests.requests[-1].trigger_type == DeploymentTrigger.REMOVE


async def test_request_teardown_deployment_in_progress_raises(setup: DeploymentSetup) -> None:
    await _deploy(setup, setup.service, DeploymentStatus.BUILDING)

    with pytest.raises(DeploymentInProgressError):
        await _teardown(setup).request_teardown([setup.service], OWNER)

    assert len(setup.requests.requests) == 1


async def test_request_teardown_one_service_in_progress_creates_nothing_for_others(
    setup: DeploymentSetup,
) -> None:
    await _deploy(setup, setup.service, DeploymentStatus.SUCCEEDED)
    busy = await _other_service(setup, "api")
    await _deploy(setup, busy, DeploymentStatus.DEPLOYING)
    requests_before = len(setup.requests.requests)

    with pytest.raises(DeploymentInProgressError) as exc_info:
        await _teardown(setup).request_teardown([setup.service, busy], OWNER)

    assert exc_info.value.fields == {"service_id": busy.id}
    assert len(setup.requests.requests) == requests_before


async def test_request_teardown_many_services_requests_each_live_one(
    setup: DeploymentSetup,
) -> None:
    await _deploy(setup, setup.service, DeploymentStatus.SUCCEEDED)
    second = await _other_service(setup, "api")
    await _deploy(setup, second, DeploymentStatus.SUCCEEDED)
    idle = await _other_service(setup, "worker")

    created = await _teardown(setup).request_teardown([setup.service, second, idle], OWNER)

    removals = [r for r in setup.requests.requests if r.trigger_type == DeploymentTrigger.REMOVE]
    assert created == 2
    assert {r.service_id for r in removals} == {setup.service.id, second.id}


async def test_request_teardown_without_services_creates_nothing(setup: DeploymentSetup) -> None:
    assert await _teardown(setup).request_teardown([], OWNER) == 0
