import pytest

from app.clients.source_repository_client import CommitInfo
from app.core.exceptions import (
    DeploymentInProgressError,
    DeploymentRequestNotFoundError,
    InvalidInputError,
    NoSucceededDeploymentError,
    ServiceNotFoundError,
)
from app.enums import Builder, BuildStatus, DeploymentStatus, DeploymentTrigger, JobKind
from app.models.deployment_request import DeploymentRequest
from app.models.service import Service
from tests.fakes_deployment import FULL_NAME, HEAD_SHA, OWNER, DeploymentSetup


@pytest.fixture
async def setup() -> DeploymentSetup:
    return await DeploymentSetup().build()


IMAGE_DIGEST = "sha256:" + "a" * 64


async def _finished_request(
    setup: DeploymentSetup,
    status: DeploymentStatus = DeploymentStatus.SUCCEEDED,
    *,
    sha: str = "old1234",
    message: str = "fix: old",
) -> DeploymentRequest:
    """다른 커밋을 배포했다가 끝난 요청. 재배포·롤백·재시작의 원본으로 쓴다.

    SUCCEEDED 면 Worker 가 이미지를 만든 것처럼 빌드도 성공 상태로 채운다.
    """
    setup.github.heads[(FULL_NAME, "main")] = CommitInfo(sha, message)
    request = await setup.manual_service().create_deployment_request(
        OWNER, setup.service.id, trigger_type=DeploymentTrigger.MANUAL
    )
    request.status = status
    if status == DeploymentStatus.SUCCEEDED:
        build = setup.builds.builds[-1]
        build.builder = Builder.RAILPACK
        build.source_sha = sha
        build.image_repository = "123.dkr.ecr.ap-northeast-2.amazonaws.com/iris/services/1"
        build.image_tag = f"b-{build.id}"
        build.deploy_config = {"healthcheck_timeout": 60}
        build.succeed(IMAGE_DIGEST)
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
    assert request.source_deployment_request_id == source.id
    # 재배포는 같은 커밋을 다시 빌드한다.
    assert (setup.jobs.jobs[-1].kind, request.status) == (JobKind.BUILD, DeploymentStatus.QUEUED)


async def test_create_deployment_request_rollback_reuses_source_image_without_build(
    setup: DeploymentSetup,
) -> None:
    source = await _finished_request(setup, DeploymentStatus.SUCCEEDED)
    source.variables_snapshot = {"LOG_LEVEL": "debug"}
    jobs_before = len(setup.jobs.jobs)

    request = await setup.manual_service().create_deployment_request(
        OWNER,
        setup.service.id,
        trigger_type=DeploymentTrigger.ROLLBACK,
        source_deployment_request_id=source.id,
    )

    assert (request.trigger_type, request.source_sha, request.source_commit_message) == (
        DeploymentTrigger.ROLLBACK,
        "old1234",
        "fix: old",
    )
    assert request.source_deployment_request_id == source.id
    assert request.variables_snapshot == {"LOG_LEVEL": "debug"}
    assert request.status == DeploymentStatus.DEPLOYING
    copied = setup.builds.builds[-1]
    source_build = setup.builds.builds[0]
    assert copied.deployment_request_id == request.id
    assert copied.status == BuildStatus.SUCCEEDED
    assert (copied.image_repository, copied.image_tag, copied.image_digest) == (
        source_build.image_repository,
        source_build.image_tag,
        source_build.image_digest,
    )
    assert (copied.builder, copied.deploy_config) == (Builder.RAILPACK, {"healthcheck_timeout": 60})
    assert copied.codebuild_build_id is None
    new_jobs = setup.jobs.jobs[jobs_before:]
    assert [(j.kind, j.payload) for j in new_jobs] == [(JobKind.DEPLOY, {"build_id": copied.id})]
    assert [
        (h.from_status, h.to_status)
        for h in setup.histories.histories
        if h.deployment_request_id == request.id
    ] == [(None, DeploymentStatus.QUEUED), (DeploymentStatus.QUEUED, DeploymentStatus.DEPLOYING)]


async def test_create_deployment_request_rollback_to_source_without_image_raises_invalid_input(
    setup: DeploymentSetup,
) -> None:
    source = await _finished_request(setup, DeploymentStatus.SUCCEEDED)
    setup.builds.builds[0].image_digest = None

    with pytest.raises(InvalidInputError):
        await setup.manual_service().create_deployment_request(
            OWNER,
            setup.service.id,
            trigger_type=DeploymentTrigger.ROLLBACK,
            source_deployment_request_id=source.id,
        )

    assert len(setup.requests.requests) == 1


async def test_create_deployment_request_restart_reuses_latest_succeeded_image(
    setup: DeploymentSetup,
) -> None:
    await _finished_request(setup, DeploymentStatus.SUCCEEDED, sha="old1234", message="old")
    live = await _finished_request(setup, DeploymentStatus.SUCCEEDED, sha="new5678", message="new")
    await _finished_request(setup, DeploymentStatus.FAILED, sha="bad0000", message="bad")

    request = await setup.manual_service().create_deployment_request(
        OWNER, setup.service.id, trigger_type=DeploymentTrigger.RESTART
    )

    assert request.trigger_type == DeploymentTrigger.RESTART
    assert request.source_deployment_request_id == live.id
    assert (request.source_sha, request.source_commit_message) == ("new5678", "new")
    assert request.status == DeploymentStatus.DEPLOYING
    job = setup.jobs.jobs[-1]
    assert (job.kind, job.payload) == (JobKind.DEPLOY, {"build_id": setup.builds.builds[-1].id})


async def test_create_deployment_request_restart_without_succeeded_deployment_raises_conflict(
    setup: DeploymentSetup,
) -> None:
    await _finished_request(setup, DeploymentStatus.FAILED)

    with pytest.raises(NoSucceededDeploymentError):
        await setup.manual_service().create_deployment_request(
            OWNER, setup.service.id, trigger_type=DeploymentTrigger.RESTART
        )

    assert len(setup.requests.requests) == 1


async def test_create_deployment_request_restart_while_active_raises_in_progress(
    setup: DeploymentSetup,
) -> None:
    live = await _finished_request(setup, DeploymentStatus.SUCCEEDED)
    await setup.manual_service().create_deployment_request(
        OWNER, setup.service.id, trigger_type=DeploymentTrigger.MANUAL
    )

    with pytest.raises(DeploymentInProgressError):
        await setup.manual_service().create_deployment_request(
            OWNER, setup.service.id, trigger_type=DeploymentTrigger.RESTART
        )

    assert [r.source_deployment_request_id for r in setup.requests.requests] == [None, None]
    assert live.status == DeploymentStatus.SUCCEEDED


async def test_create_deployment_request_restart_same_idempotency_key_returns_first_request(
    setup: DeploymentSetup,
) -> None:
    await _finished_request(setup, DeploymentStatus.SUCCEEDED)
    service = setup.manual_service()
    first = await service.create_deployment_request(
        OWNER, setup.service.id, trigger_type=DeploymentTrigger.RESTART, idempotency_key="r-1"
    )

    replayed = await service.create_deployment_request(
        OWNER, setup.service.id, trigger_type=DeploymentTrigger.RESTART, idempotency_key="r-1"
    )

    assert replayed.id == first.id
    assert len(setup.requests.requests) == 2


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


async def test_create_deployment_request_remove_takes_live_deployment_down_without_build(
    setup: DeploymentSetup,
) -> None:
    live = await _finished_request(setup, DeploymentStatus.SUCCEEDED)
    builds_before = len(setup.builds.builds)

    request = await setup.manual_service().create_deployment_request(
        OWNER, setup.service.id, trigger_type=DeploymentTrigger.REMOVE
    )

    assert (request.trigger_type, request.status) == (
        DeploymentTrigger.REMOVE,
        DeploymentStatus.DEPLOYING,
    )
    assert request.source_deployment_request_id == live.id
    assert len(setup.builds.builds) == builds_before
    assert setup.jobs.jobs[-1].kind == JobKind.REMOVE
    assert [
        (h.from_status, h.to_status)
        for h in setup.histories.histories
        if h.deployment_request_id == request.id
    ] == [(None, DeploymentStatus.QUEUED), (DeploymentStatus.QUEUED, DeploymentStatus.DEPLOYING)]


async def test_create_deployment_request_remove_without_succeeded_deployment_raises_conflict(
    setup: DeploymentSetup,
) -> None:
    await _finished_request(setup, DeploymentStatus.FAILED)

    with pytest.raises(NoSucceededDeploymentError):
        await setup.manual_service().create_deployment_request(
            OWNER, setup.service.id, trigger_type=DeploymentTrigger.REMOVE
        )


@pytest.mark.parametrize("trigger_type", [DeploymentTrigger.REMOVE, DeploymentTrigger.RESTART])
async def test_create_deployment_request_after_remove_has_no_running_deployment(
    setup: DeploymentSetup, trigger_type: DeploymentTrigger
) -> None:
    await _finished_request(setup, DeploymentStatus.SUCCEEDED)
    removal = await setup.manual_service().create_deployment_request(
        OWNER, setup.service.id, trigger_type=DeploymentTrigger.REMOVE
    )
    removal.status = DeploymentStatus.SUCCEEDED

    with pytest.raises(NoSucceededDeploymentError):
        await setup.manual_service().create_deployment_request(
            OWNER, setup.service.id, trigger_type=trigger_type
        )


async def test_create_deployment_request_rollback_after_remove_brings_old_image_back(
    setup: DeploymentSetup,
) -> None:
    old = await _finished_request(setup, DeploymentStatus.SUCCEEDED)
    removal = await setup.manual_service().create_deployment_request(
        OWNER, setup.service.id, trigger_type=DeploymentTrigger.REMOVE
    )
    removal.status = DeploymentStatus.SUCCEEDED

    request = await setup.manual_service().create_deployment_request(
        OWNER,
        setup.service.id,
        trigger_type=DeploymentTrigger.ROLLBACK,
        source_deployment_request_id=old.id,
    )

    assert (request.trigger_type, request.status) == (
        DeploymentTrigger.ROLLBACK,
        DeploymentStatus.DEPLOYING,
    )


async def test_create_deployment_request_remove_while_active_raises_in_progress(
    setup: DeploymentSetup,
) -> None:
    await _finished_request(setup, DeploymentStatus.SUCCEEDED)
    await setup.manual_service().create_deployment_request(
        OWNER, setup.service.id, trigger_type=DeploymentTrigger.MANUAL
    )

    with pytest.raises(DeploymentInProgressError):
        await setup.manual_service().create_deployment_request(
            OWNER, setup.service.id, trigger_type=DeploymentTrigger.REMOVE
        )
