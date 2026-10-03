import gzip
import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from app.core.exceptions import (
    ExternalError,
    InvalidInputError,
    ServiceNotFoundError,
    UploadNotGzipError,
    UploadTooLargeError,
)
from app.models.service_upload import ServiceUpload
from app.services.upload_service import (
    EXPIRED_UPLOAD_RETENTION,
    UPLOAD_TTL,
    UploadService,
)
from tests.fakes_deployment import OWNER, DeploymentSetup
from tests.fakes_upload import storage_failure

MAX_BYTES = 1000


class TrackedBody:
    """청크 목록을 내주는 본문. 몇 번 읽었는지 센다."""

    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks
        self.reads = 0

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.reads += 1
            yield chunk


@pytest.fixture
async def setup() -> DeploymentSetup:
    return await DeploymentSetup().build()


def _service(setup: DeploymentSetup, max_bytes: int = MAX_BYTES) -> UploadService:
    return UploadService(
        setup.session,  # type: ignore[arg-type]
        setup.services,  # type: ignore[arg-type]
        setup.uploads,  # type: ignore[arg-type]
        setup.storage,
        max_bytes,
    )


def _archive() -> bytes:
    return gzip.compress(b"x" * 10)


async def test_create_upload_stores_archive_and_records_metadata(setup: DeploymentSetup) -> None:
    data = _archive()
    body = TrackedBody(data[:5], data[5:])

    upload = await _service(setup).create_upload(
        OWNER, setup.service.id, content_length=len(data), body=body
    )

    assert upload.size_bytes == len(data)
    assert upload.sha256 == hashlib.sha256(data).hexdigest()
    assert upload.service_id == setup.service.id
    assert upload.uploaded_by == OWNER
    assert upload.consumed_at is None
    assert len(upload.public_id) >= 43
    assert upload.storage_key == f"uploads/{upload.public_id}.tar.gz"
    assert setup.storage.objects[upload.storage_key] == data
    assert setup.session.commit_count == 2  # 소유 확인 뒤 연결을 풀고, 행을 남기며 한 번 더


async def test_create_upload_expires_after_ttl(setup: DeploymentSetup) -> None:
    data = _archive()
    before = datetime.now(UTC)

    upload = await _service(setup).create_upload(
        OWNER, setup.service.id, content_length=len(data), body=TrackedBody(data)
    )

    assert before + UPLOAD_TTL <= upload.expires_at <= datetime.now(UTC) + UPLOAD_TTL


async def test_create_upload_hashes_across_many_chunks_larger_than_write_block(
    setup: DeploymentSetup,
) -> None:
    data = gzip.compress(bytes(range(256)) * 20_000)
    chunks = [data[i : i + 4096] for i in range(0, len(data), 4096)]

    upload = await _service(setup, max_bytes=len(data)).create_upload(
        OWNER, setup.service.id, content_length=len(data), body=TrackedBody(*chunks)
    )

    assert upload.sha256 == hashlib.sha256(data).hexdigest()
    assert setup.storage.objects[upload.storage_key] == data


async def test_create_upload_issues_distinct_unguessable_ids(setup: DeploymentSetup) -> None:
    data = _archive()
    service = _service(setup)

    ids = {
        (
            await service.create_upload(
                OWNER, setup.service.id, content_length=len(data), body=TrackedBody(data)
            )
        ).public_id
        for _ in range(5)
    }

    assert len(ids) == 5


async def test_create_upload_for_service_of_another_user_is_not_found(
    setup: DeploymentSetup,
) -> None:
    body = TrackedBody(_archive())

    with pytest.raises(ServiceNotFoundError):
        await _service(setup).create_upload(999, setup.service.id, content_length=10, body=body)

    assert body.reads == 0
    assert setup.storage.objects == {}


async def test_create_upload_for_unknown_service_is_not_found(setup: DeploymentSetup) -> None:
    with pytest.raises(ServiceNotFoundError):
        await _service(setup).create_upload(OWNER, 424242, content_length=10, body=TrackedBody())


async def test_create_upload_without_content_length_is_rejected_before_reading(
    setup: DeploymentSetup,
) -> None:
    body = TrackedBody(_archive())

    with pytest.raises(InvalidInputError, match="Content-Length"):
        await _service(setup).create_upload(OWNER, setup.service.id, content_length=None, body=body)

    assert body.reads == 0


async def test_create_upload_with_empty_body_is_rejected(setup: DeploymentSetup) -> None:
    with pytest.raises(InvalidInputError, match="empty"):
        await _service(setup).create_upload(
            OWNER, setup.service.id, content_length=0, body=TrackedBody()
        )


async def test_create_upload_over_limit_is_rejected_by_content_length_before_reading(
    setup: DeploymentSetup,
) -> None:
    body = TrackedBody(_archive())

    with pytest.raises(UploadTooLargeError) as raised:
        await _service(setup).create_upload(
            OWNER, setup.service.id, content_length=MAX_BYTES + 1, body=body
        )

    assert body.reads == 0
    assert raised.value.fields["max_bytes"] == MAX_BYTES
    assert setup.storage.objects == {}


async def test_create_upload_at_exactly_the_limit_is_accepted(setup: DeploymentSetup) -> None:
    data = _archive()
    limit = len(data)

    upload = await _service(setup, max_bytes=limit).create_upload(
        OWNER, setup.service.id, content_length=limit, body=TrackedBody(data)
    )

    assert upload.size_bytes == limit


async def test_create_upload_that_is_not_gzip_is_rejected_on_the_first_chunk(
    setup: DeploymentSetup,
) -> None:
    body = TrackedBody(b"PK\x03\x04 a zip file", b"never read")

    with pytest.raises(UploadNotGzipError):
        await _service(setup).create_upload(OWNER, setup.service.id, content_length=30, body=body)

    assert body.reads == 1
    assert setup.storage.objects == {}


async def test_create_upload_detects_magic_bytes_split_across_chunks(
    setup: DeploymentSetup,
) -> None:
    body = TrackedBody(b"\x1f", b"\x8b", b"\x08rest")

    upload = await _service(setup).create_upload(
        OWNER, setup.service.id, content_length=7, body=body
    )

    assert upload.size_bytes == 7


async def test_create_upload_with_single_byte_body_is_not_gzip(setup: DeploymentSetup) -> None:
    with pytest.raises(UploadNotGzipError):
        await _service(setup).create_upload(
            OWNER, setup.service.id, content_length=1, body=TrackedBody(b"\x1f")
        )


async def test_create_upload_longer_than_declared_is_rejected(setup: DeploymentSetup) -> None:
    data = _archive()

    with pytest.raises(InvalidInputError, match="longer"):
        await _service(setup).create_upload(
            OWNER, setup.service.id, content_length=len(data) - 1, body=TrackedBody(data)
        )

    assert setup.storage.objects == {}


async def test_create_upload_shorter_than_declared_is_rejected(setup: DeploymentSetup) -> None:
    data = _archive()

    with pytest.raises(InvalidInputError, match="shorter"):
        await _service(setup).create_upload(
            OWNER, setup.service.id, content_length=len(data) + 5, body=TrackedBody(data)
        )

    assert setup.storage.objects == {}
    assert setup.uploads.uploads == []


async def test_create_upload_storage_failure_leaves_no_row(setup: DeploymentSetup) -> None:
    setup.storage.fail_with = storage_failure()
    data = _archive()

    with pytest.raises(ExternalError):
        await _service(setup).create_upload(
            OWNER, setup.service.id, content_length=len(data), body=TrackedBody(data)
        )

    assert setup.uploads.uploads == []


async def test_create_upload_deletes_long_expired_unused_rows_but_keeps_used_ones(
    setup: DeploymentSetup,
) -> None:
    now = datetime.now(UTC)

    def stale(public_id: str, consumed: bool) -> ServiceUpload:
        return ServiceUpload(
            public_id=public_id,
            service_id=setup.service.id,
            uploaded_by=OWNER,
            size_bytes=1,
            sha256="0" * 64,
            storage_key=f"uploads/{public_id}.tar.gz",
            expires_at=now - EXPIRED_UPLOAD_RETENTION - timedelta(hours=1),
            consumed_at=now - timedelta(days=3) if consumed else None,
        )

    recent_expired = stale("recent", consumed=False)
    recent_expired.expires_at = now - timedelta(hours=2)
    for upload in (stale("old-unused", False), stale("old-used", True), recent_expired):
        await setup.uploads.add(upload)
    data = _archive()

    await _service(setup).create_upload(
        OWNER, setup.service.id, content_length=len(data), body=TrackedBody(data)
    )

    assert sorted(u.public_id for u in setup.uploads.uploads if u.size_bytes == 1) == [
        "old-used",
        "recent",
    ]
