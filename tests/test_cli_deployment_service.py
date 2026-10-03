"""`CLI` 트리거(업로드를 소스로 쓰는 배포 요청)의 서비스 계층 테스트. DB 없이 fake 로 검증한다."""

from datetime import UTC, datetime, timedelta

import pytest

from app.core.exceptions import (
    DeploymentInProgressError,
    InvalidInputError,
    UploadNotFoundError,
    UploadUnavailableError,
)
from app.enums import Builder, DeploymentStatus, DeploymentTrigger, JobKind
from app.models.deployment_request import DeploymentRequest
from app.models.service_upload import ServiceUpload
from tests.fakes_deployment import OWNER, DeploymentSetup

SHA256 = "3fa9c2d1b7e4" + "0" * 52
IMAGE_DIGEST = "sha256:" + "a" * 64


@pytest.fixture
async def setup() -> DeploymentSetup:
    return await DeploymentSetup().build()


async def _upload(
    setup: DeploymentSetup,
    public_id: str = "up-1",
    *,
    service_id: int | None = None,
    expires_in: timedelta = timedelta(hours=1),
    consumed: bool = False,
) -> ServiceUpload:
    now = datetime.now(UTC)
    return await setup.uploads.add(
        ServiceUpload(
            public_id=public_id,
            service_id=service_id if service_id is not None else setup.service.id,
            uploaded_by=OWNER,
            size_bytes=1234,
            sha256=SHA256,
            storage_key=f"uploads/{public_id}.tar.gz",
            expires_at=now + expires_in,
            consumed_at=now if consumed else None,
        )
    )


async def _create(
    setup: DeploymentSetup, upload_id: str | None = "up-1", key: str | None = None
) -> DeploymentRequest:
    return await setup.manual_service().create_deployment_request(
        OWNER,
        setup.service.id,
        trigger_type=DeploymentTrigger.CLI,
        upload_id=upload_id,
        idempotency_key=key,
    )


async def test_cli_request_binds_upload_and_queues_a_build(setup: DeploymentSetup) -> None:
    upload = await _upload(setup)

    request = await _create(setup)

    assert request.trigger_type == DeploymentTrigger.CLI
    assert request.status == DeploymentStatus.QUEUED
    assert request.service_upload_id == upload.id
    assert request.source_sha == "upload-3fa9c2d1b7e4"
    assert request.source_commit_message is None
    assert request.requested_by == OWNER
    assert upload.consumed_at is not None
    job = setup.jobs.jobs[0]
    assert (job.kind, job.payload) == (JobKind.BUILD, {"build_id": setup.builds.builds[0].id})
    assert setup.session.commit_count == 1


async def test_cli_request_does_not_call_github(setup: DeploymentSetup) -> None:
    await _upload(setup)

    await _create(setup)

    assert setup.github.token_requests == []


async def test_cli_request_snapshots_current_variables_like_other_sources(
    setup: DeploymentSetup,
) -> None:
    await setup.variables.replace_all(setup.service.id, {"API_KEY": "enc(secret)"})
    await _upload(setup)

    request = await _create(setup)

    assert request.variables_snapshot == {"API_KEY": "enc(secret)"}


async def test_cli_request_without_upload_id_is_invalid(setup: DeploymentSetup) -> None:
    with pytest.raises(InvalidInputError, match="upload is required"):
        await _create(setup, upload_id=None)


async def test_cli_request_with_unknown_upload_is_not_found(setup: DeploymentSetup) -> None:
    with pytest.raises(UploadNotFoundError):
        await _create(setup, upload_id="nope")


async def test_cli_request_with_upload_of_another_service_is_not_found(
    setup: DeploymentSetup,
) -> None:
    await _upload(setup, service_id=setup.service.id + 100)

    with pytest.raises(UploadNotFoundError):
        await _create(setup)


async def test_cli_request_with_expired_upload_is_unavailable(setup: DeploymentSetup) -> None:
    upload = await _upload(setup, expires_in=timedelta(seconds=-1))

    with pytest.raises(UploadUnavailableError):
        await _create(setup)

    assert upload.consumed_at is None
    assert setup.requests.requests == []


async def test_cli_request_with_used_upload_is_unavailable(setup: DeploymentSetup) -> None:
    await _upload(setup)
    first = await _create(setup)
    setup.finish_active_requests()

    with pytest.raises(UploadUnavailableError):
        await _create(setup)

    assert [r.id for r in setup.requests.requests] == [first.id]


async def test_cli_request_while_active_conflicts_and_returns_the_upload(
    setup: DeploymentSetup,
) -> None:
    upload = await _upload(setup)
    await setup.manual_service().create_deployment_request(
        OWNER, setup.service.id, trigger_type=DeploymentTrigger.MANUAL
    )

    with pytest.raises(DeploymentInProgressError):
        await _create(setup)

    # 요청을 만들지 못했으니 업로드는 그대로 쓸 수 있어야 한다.
    assert upload.consumed_at is None
    assert setup.session.rollback_count == 1
    setup.finish_active_requests()
    request = await _create(setup)
    assert request.service_upload_id == upload.id


async def test_cli_request_retry_with_same_idempotency_key_replays_without_claiming_again(
    setup: DeploymentSetup,
) -> None:
    await _upload(setup)
    first = await _create(setup, key="up-up-1")

    replayed = await _create(setup, key="up-up-1")

    assert replayed is first
    assert len(setup.requests.requests) == 1


async def test_redeploy_of_cli_deployment_is_rejected(setup: DeploymentSetup) -> None:
    await _upload(setup)
    source = await _create(setup)
    source.status = DeploymentStatus.SUCCEEDED

    with pytest.raises(InvalidInputError, match="cannot be rebuilt"):
        await setup.manual_service().create_deployment_request(
            OWNER,
            setup.service.id,
            trigger_type=DeploymentTrigger.REDEPLOY,
            source_deployment_request_id=source.id,
        )


async def test_rollback_and_restart_of_cli_deployment_reuse_its_image(
    setup: DeploymentSetup,
) -> None:
    await _upload(setup)
    source = await _create(setup)
    source.status = DeploymentStatus.SUCCEEDED
    build = setup.builds.builds[-1]
    build.builder = Builder.DOCKERFILE
    build.source_sha = source.source_sha
    build.image_repository = "123.dkr.ecr.ap-northeast-2.amazonaws.com/iris/services/1"
    build.image_tag = f"b-{build.id}"
    build.succeed(IMAGE_DIGEST)

    rollback = await setup.manual_service().create_deployment_request(
        OWNER,
        setup.service.id,
        trigger_type=DeploymentTrigger.ROLLBACK,
        source_deployment_request_id=source.id,
    )

    assert rollback.status == DeploymentStatus.DEPLOYING
    assert rollback.source_sha == source.source_sha
    # 업로드는 CLI 요청 하나에만 묶인다. 이미지를 다시 쓰는 요청은 업로드를 가리키지 않는다.
    assert rollback.service_upload_id is None
    rollback.status = DeploymentStatus.SUCCEEDED
    restart = await setup.manual_service().create_deployment_request(
        OWNER, setup.service.id, trigger_type=DeploymentTrigger.RESTART
    )
    assert restart.status == DeploymentStatus.DEPLOYING
    assert restart.source_sha == source.source_sha

    # 롤백·재시작으로 만든 요청을 원본으로 한 재배포도 소스가 없으니 거절한다.
    restart.status = DeploymentStatus.SUCCEEDED
    with pytest.raises(InvalidInputError, match="cannot be rebuilt"):
        await setup.manual_service().create_deployment_request(
            OWNER,
            setup.service.id,
            trigger_type=DeploymentTrigger.REDEPLOY,
            source_deployment_request_id=restart.id,
        )


async def test_manual_trigger_with_upload_id_ignores_it_and_keeps_upload(
    setup: DeploymentSetup,
) -> None:
    upload = await _upload(setup)

    await setup.manual_service().create_deployment_request(
        OWNER,
        setup.service.id,
        trigger_type=DeploymentTrigger.MANUAL,
        upload_id=upload.public_id,
    )

    assert upload.consumed_at is None
