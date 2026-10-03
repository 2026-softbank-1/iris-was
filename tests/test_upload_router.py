import gzip
import hashlib
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.requests import ClientDisconnect

from app.core.config import Settings
from app.core.exceptions import InvalidInputError
from app.dependencies import get_current_user, get_settings, get_upload_service
from app.main import app
from app.models.user import User
from app.routers.upload_router import _read_body
from app.services.upload_service import UploadService
from tests.fakes_deployment import OWNER, DeploymentSetup
from tests.fakes_upload import storage_failure

MAX_BYTES = 4096
ARCHIVE = gzip.compress(b"print('hello')\n" * 20)


def _user(id_: int) -> User:
    user = User(github_id=1000 + id_, login=f"user{id_}")
    user.id = id_
    return user


class UploadClient(AsyncClient):
    setup: DeploymentSetup
    current: dict[str, User]

    @property
    def url(self) -> str:
        return f"/api/v1/services/{self.setup.service.id}/uploads"


@pytest.fixture
async def client() -> AsyncIterator[UploadClient]:
    setup = await DeploymentSetup().build()
    current = {"user": _user(OWNER)}

    def upload_service() -> UploadService:
        return UploadService(
            setup.session,  # type: ignore[arg-type]
            setup.services,  # type: ignore[arg-type]
            setup.uploads,  # type: ignore[arg-type]
            setup.storage,
            MAX_BYTES,
        )

    app.dependency_overrides[get_current_user] = lambda: current["user"]
    app.dependency_overrides[get_upload_service] = upload_service
    async with UploadClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        http.setup = setup
        http.current = current
        yield http
    app.dependency_overrides.clear()


async def test_upload_returns_created_camel_case_envelope(client: UploadClient) -> None:
    response = await client.post(
        client.url, content=ARCHIVE, headers={"Content-Type": "application/gzip"}
    )

    body = response.json()
    assert response.status_code == 201
    assert body["success"] is True
    data = body["data"]
    assert set(data) == {"uploadId", "sizeBytes", "sha256", "expiresAt"}
    assert data["sizeBytes"] == len(ARCHIVE)
    assert data["sha256"] == hashlib.sha256(ARCHIVE).hexdigest()
    assert client.setup.storage.objects[f"uploads/{data['uploadId']}.tar.gz"] == ARCHIVE


async def test_upload_streams_a_chunked_body_with_known_length(client: UploadClient) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for start in range(0, len(ARCHIVE), 7):
            yield ARCHIVE[start : start + 7]

    response = await client.post(
        client.url,
        content=chunks(),
        headers={"Content-Type": "application/gzip", "Content-Length": str(len(ARCHIVE))},
    )

    assert response.status_code == 201
    assert response.json()["data"]["sha256"] == hashlib.sha256(ARCHIVE).hexdigest()


async def test_upload_without_content_length_returns_unprocessable(client: UploadClient) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        yield ARCHIVE

    response = await client.post(client.url, content=chunks())  # Transfer-Encoding: chunked

    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_INPUT"


async def test_upload_with_empty_body_returns_unprocessable(client: UploadClient) -> None:
    response = await client.post(client.url, content=b"")

    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_INPUT"


async def test_upload_over_limit_returns_payload_too_large(client: UploadClient) -> None:
    response = await client.post(client.url, content=gzip.compress(b"x" * 100) + b"0" * MAX_BYTES)

    body = response.json()
    assert response.status_code == 413
    assert body["success"] is False
    assert body["code"] == "UPLOAD_TOO_LARGE"
    assert client.setup.storage.objects == {}


async def test_upload_that_is_not_gzip_returns_unsupported_media_type(
    client: UploadClient,
) -> None:
    response = await client.post(client.url, content=b"PK\x03\x04 zip archive")

    assert response.status_code == 415
    assert response.json()["code"] == "UPLOAD_NOT_GZIP"


async def test_upload_for_another_users_service_returns_not_found(client: UploadClient) -> None:
    client.current["user"] = _user(2)

    response = await client.post(client.url, content=ARCHIVE)

    assert response.status_code == 404
    assert response.json()["code"] == "SERVICE_NOT_FOUND"
    assert client.setup.storage.objects == {}


async def test_upload_storage_failure_returns_bad_gateway(client: UploadClient) -> None:
    client.setup.storage.fail_with = storage_failure()

    response = await client.post(client.url, content=ARCHIVE)

    assert response.status_code == 502
    assert response.json()["code"] == "EXTERNAL_ERROR"


async def test_upload_without_storage_settings_returns_service_unavailable() -> None:
    # get_upload_service 는 덮어쓰지 않고 설정만 비운다. 로그인은 건너뛴다.
    app.dependency_overrides[get_current_user] = lambda: _user(OWNER)
    app.dependency_overrides[get_settings] = lambda: Settings(
        database_url="postgresql+asyncpg://t:t@127.0.0.1:1/t", aws_region=None, artifact_bucket=None
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
            response = await http.post("/api/v1/services/1/uploads", content=ARCHIVE)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json()["code"] == "NOT_CONFIGURED"


async def test_read_body_turns_client_disconnect_into_a_domain_error() -> None:
    class DroppedRequest:
        async def stream(self) -> AsyncIterator[bytes]:
            yield b"first"
            raise ClientDisconnect

    chunks: list[bytes] = []
    request: Any = DroppedRequest()

    with pytest.raises(InvalidInputError, match="interrupted"):
        async for chunk in _read_body(request):
            chunks.append(chunk)

    assert chunks == [b"first"]
