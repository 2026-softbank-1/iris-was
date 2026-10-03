"""ArtifactStore 의 업로드 저장·내려받기. 가짜 S3 로 호출 인자와 오류 변환을 본다."""

from pathlib import Path
from typing import Any

import pytest
from boto3.exceptions import RetriesExceededError, S3TransferFailedError, S3UploadFailedError
from botocore.exceptions import ClientError, EndpointConnectionError

from app.clients.aws_clients import ArtifactStore
from app.core.exceptions import ExternalError, NotFoundError


class FakeS3:
    def __init__(self) -> None:
        self.uploaded: list[dict[str, Any]] = []
        self.content = b"archive-bytes"
        self.error: Exception | None = None

    def upload_file(self, **kwargs: Any) -> None:
        if self.error is not None:
            raise self.error
        self.uploaded.append(kwargs)

    def download_file(self, *, Bucket: str, Key: str, Filename: str) -> None:  # noqa: N803
        if self.error is not None:
            raise self.error
        Path(Filename).write_bytes(self.content)


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "x"}}, "HeadObject")


@pytest.fixture
def s3() -> FakeS3:
    return FakeS3()


@pytest.fixture
def store(s3: FakeS3) -> ArtifactStore:
    return ArtifactStore("ap-northeast-2", "iris-artifacts", client=s3)


async def test_put_upload_writes_under_uploads_prefix_with_gzip_content_type(
    store: ArtifactStore, s3: FakeS3, tmp_path: Path
) -> None:
    archive = tmp_path / "upload.tar.gz"
    archive.write_bytes(b"x")

    key = await store.put_upload("abc-DEF_123", archive)

    assert key == "uploads/abc-DEF_123.tar.gz"
    assert s3.uploaded == [
        {
            "Filename": str(archive),
            "Bucket": "iris-artifacts",
            "Key": "uploads/abc-DEF_123.tar.gz",
            "ExtraArgs": {"ContentType": "application/gzip"},
        }
    ]


async def test_put_upload_failure_is_external_error(
    store: ArtifactStore, s3: FakeS3, tmp_path: Path
) -> None:
    s3.error = S3UploadFailedError("denied")

    with pytest.raises(ExternalError):
        await store.put_upload("id", tmp_path / "upload.tar.gz")


async def test_download_upload_writes_the_object_to_dest(
    store: ArtifactStore, tmp_path: Path
) -> None:
    dest = tmp_path / "upload.tar.gz"

    await store.download_upload("uploads/id.tar.gz", dest)

    assert dest.read_bytes() == b"archive-bytes"


@pytest.mark.parametrize("code", ["404", "NoSuchKey", "NotFound"])
async def test_download_upload_missing_object_is_not_found(
    store: ArtifactStore, s3: FakeS3, tmp_path: Path, code: str
) -> None:
    s3.error = _client_error(code)

    with pytest.raises(NotFoundError):
        await store.download_upload("uploads/id.tar.gz", tmp_path / "x")


@pytest.mark.parametrize(
    "error",
    [
        _client_error("403"),
        _client_error("InternalError"),
        EndpointConnectionError(endpoint_url="https://s3.example"),
        S3TransferFailedError("failed"),
        RetriesExceededError(Exception("retries")),
    ],
)
async def test_download_upload_other_failures_are_retryable_external_errors(
    store: ArtifactStore, s3: FakeS3, tmp_path: Path, error: Exception
) -> None:
    s3.error = error

    with pytest.raises(ExternalError) as raised:
        await store.download_upload("uploads/id.tar.gz", tmp_path / "x")

    assert raised.value.retryable is True
