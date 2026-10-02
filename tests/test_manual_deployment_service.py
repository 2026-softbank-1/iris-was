import pytest

from app.clients.source_repository_client import CommitInfo
from app.core.exceptions import (
    DeploymentInProgressError,
    DeploymentRequestNotFoundError,
    InvalidInputError,
    ServiceNotFoundError,
)
from app.enums import DeploymentStatus, DeploymentTrigger, JobKind
from app.models.deployment_request import DeploymentRequest
from app.models.service import Service
from tests.fakes_deployment import FULL_NAME, HEAD_SHA, OWNER, DeploymentSetup


@pytest.fixture
async def setup() -> DeploymentSetup:
    return await DeploymentSetup().build()


async def _finished_request(
    setup: DeploymentSetup, status: DeploymentStatus = DeploymentStatus.SUCCEEDED
) -> DeploymentRequest:
    """다른 커밋을 배포했다가 끝난 요청. 재배포·롤백의 원본으로 쓴다."""
    setup.github.heads[(FULL_NAME, "main")] = CommitInfo("old1234", "fix: old")
    request = await setup.manual_service().create_deployment_request(
        OWNER, setup.service.id, trigger_type=DeploymentTrigger.MANUAL
    )
    request.status = status
    setup.github.heads[(FULL_NAME, "main")] = CommitInfo(HEAD_SHA, "feat: add login")
    return request


async def test_create_deployment_request_manual_uses_branch_head(setup: DeploymentSetup) -> None:
    request = await setup.manual_service().create_deployment_request(
        OWNER, setup.service.id, trigger_type=DeploymentTrigger.MANUAL
    )

    assert (request.source_sha, request.source_commit_message) == (HEAD_SHA, "feat: add login")
    assert (request.trigger_type, request.requested_by) == (DeploymentTrigger.MANUAL, OWNER)
    assert request.status == DeploymentStatus.QUEUED
    job = setup.jobs.jobs[0]
    assert (job.kind, job.payload) == (JobKind.BUILD, {"build_id": setup.builds.builds[0].id})
    assert [(h.from_status, h.to_status) for h in setup.histories.histories] == [
        (None, DeploymentStatus.QUEUED)
    ]
    assert setup.session.commit_count == 1


async def test_create_deployment_request_manual_with_source_sha_skips_github(
    setup: DeploymentSetup,
) -> None:
    setup.github.heads.clear()

    request = await setup.manual_service().create_deployment_request(
        OWNER, setup.service.id, trigger_type=DeploymentTrigger.MANUAL, source_sha="deadbeef"
    )

    assert (request.source_sha, request.source_commit_message) == ("deadbeef", None)


async def test_create_deployment_request_branch_missing_raises_invalid_input(
    setup: DeploymentSetup,
) -> None:
    setup.github.heads.clear()

    with pytest.raises(InvalidInputError):
        await setup.manual_service().create_deployment_request(
            OWNER, setup.service.id, trigger_type=DeploymentTrigger.MANUAL
        )

    assert setup.requests.requests == []
    assert setup.session.commit_count == 0


async def test_create_deployment_request_redeploy_copies_source_commit(
    setup: DeploymentSetup,
) -> None:
    source = await _finished_request(setup, DeploymentStatus.FAILED)

    request = await setup.manual_service().create_deployment_request(
        OWNER,
        setup.service.id,
        trigger_type=DeploymentTrigger.REDEPLOY,
        source_deployment_request_id=source.id,
    )

    assert request.id != source.id
    assert (request.trigger_type, request.source_sha, request.source_commit_message) == (
        DeploymentTrigger.REDEPLOY,
        "old1234",
        "fix: old",
    )


async def test_create_deployment_request_rollback_to_succeeded_copies_source_commit(
    setup: DeploymentSetup,
) -> None:
    source = await _finished_request(setup, DeploymentStatus.SUCCEEDED)

    request = await setup.manual_service().create_deployment_request(
        OWNER,
        setup.service.id,
        trigger_type=DeploymentTrigger.ROLLBACK,
        source_deployment_request_id=source.id,
    )

    assert (request.trigger_type, request.source_sha) == (DeploymentTrigger.ROLLBACK, "old1234")


async def test_create_deployment_request_rollback_to_failed_raises_invalid_input(
    setup: DeploymentSetup,
) -> None:
    source = await _finished_request(setup, DeploymentStatus.FAILED)

    with pytest.raises(InvalidInputError):
        await setup.manual_service().create_deployment_request(
            OWNER,
            setup.service.id,
            trigger_type=DeploymentTrigger.ROLLBACK,
            source_deployment_request_id=source.id,
        )

    assert len(setup.requests.requests) == 1


@pytest.mark.parametrize("trigger_type", [DeploymentTrigger.REDEPLOY, DeploymentTrigger.ROLLBACK])
async def test_create_deployment_request_without_source_deployment_raises_invalid_input(
    setup: DeploymentSetup, trigger_type: DeploymentTrigger
) -> None:
    with pytest.raises(InvalidInputError):
        await setup.manual_service().create_deployment_request(
            OWNER, setup.service.id, trigger_type=trigger_type
        )


async def test_create_deployment_request_source_of_other_service_raises_not_found(
    setup: DeploymentSetup,
) -> None:
    source = await _finished_request(setup)
    other = await setup.services.save(
        Service(
            project_id=setup.service.project_id,
            name="api",
            source_repository_url=setup.service.source_repository_url,
            github_installation_id=setup.service.github_installation_id,
            source_branch="main",
            is_auto_deploy=True,
        )
    )

    with pytest.raises(DeploymentRequestNotFoundError):
        await setup.manual_service().create_deployment_request(
            OWNER,
            other.id,
            trigger_type=DeploymentTrigger.REDEPLOY,
            source_deployment_request_id=source.id,
        )


@pytest.mark.parametrize("trigger_type", [DeploymentTrigger.PUSH, DeploymentTrigger.CLI])
async def test_create_deployment_request_unsupported_trigger_raises_invalid_input(
    setup: DeploymentSetup, trigger_type: DeploymentTrigger
) -> None:
    with pytest.raises(InvalidInputError):
        await setup.manual_service().create_deployment_request(
            OWNER, setup.service.id, trigger_type=trigger_type
        )


async def test_create_deployment_request_while_active_raises_in_progress(
    setup: DeploymentSetup,
) -> None:
    await setup.manual_service().create_deployment_request(
        OWNER, setup.service.id, trigger_type=DeploymentTrigger.MANUAL
    )

    with pytest.raises(DeploymentInProgressError):
        await setup.manual_service().create_deployment_request(
            OWNER, setup.service.id, trigger_type=DeploymentTrigger.MANUAL
        )

    assert len(setup.requests.requests) == 1
    assert setup.session.commit_count == 1


async def test_create_deployment_request_same_idempotency_key_returns_first_request(
    setup: DeploymentSetup,
) -> None:
    service = setup.manual_service()
    first = await service.create_deployment_request(
        OWNER, setup.service.id, trigger_type=DeploymentTrigger.MANUAL, idempotency_key="click-1"
    )

    replayed = await service.create_deployment_request(
        OWNER, setup.service.id, trigger_type=DeploymentTrigger.MANUAL, idempotency_key="click-1"
    )

    assert replayed.id == first.id
    assert len(setup.requests.requests) == 1
    assert setup.session.commit_count == 1


async def test_create_deployment_request_idempotency_key_is_scoped_by_service(
    setup: DeploymentSetup,
) -> None:
    request = await setup.manual_service().create_deployment_request(
        OWNER, setup.service.id, trigger_type=DeploymentTrigger.MANUAL, idempotency_key="k"
    )

    assert request.idempotency_key == f"manual:{setup.service.id}:k"


async def test_create_deployment_request_other_users_service_raises_not_found(
    setup: DeploymentSetup,
) -> None:
    with pytest.raises(ServiceNotFoundError):
        await setup.manual_service().create_deployment_request(
            OWNER + 1, setup.service.id, trigger_type=DeploymentTrigger.MANUAL
        )
