import asyncio
import hashlib
import logging
import tempfile
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO

from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.aws_clients import UploadWriter
from app.core.exceptions import (
    InvalidInputError,
    ServiceNotFoundError,
    UploadNotGzipError,
    UploadTooLargeError,
)
from app.core.security import generate_url_token
from app.models.service_upload import ServiceUpload
from app.repositories.service_repository import ServiceRepository
from app.repositories.service_upload_repository import ServiceUploadRepository

logger = logging.getLogger(__name__)

# 업로드는 이 시간 안에 배포 요청에 쓰여야 한다. 버킷 lifecycle(1일)이 지우기 전이도록 24시간이다.
UPLOAD_TTL = timedelta(hours=24)
# 쓰이지 못하고 만료된 행은 새 업로드를 받을 때 이 기간이 지난 것부터 지운다.
EXPIRED_UPLOAD_RETENTION = timedelta(days=1)
_GZIP_MAGIC = b"\x1f\x8b"
# 받은 조각을 이만큼 모아 한 번에 쓴다. 조각마다 스레드를 거치지 않으려는 것이다.
_WRITE_BLOCK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class _ReceivedArchive:
    size_bytes: int
    sha256: str


class UploadService:
    """`likelion up` 이 올린 소스 아카이브를 받아 저장소에 두고 메타데이터를 남긴다.

    본문은 메모리에 올리지 않고 임시 파일로 받으며 sha256 을 계산한다. 아카이브 안의 내용은
    여기서 읽지 않는다. 경로 이탈 같은 검사는 소스로 쓰는 Build Worker 가 한다.
    """

    def __init__(
        self,
        session: AsyncSession,
        service_repository: ServiceRepository,
        upload_repository: ServiceUploadRepository,
        storage: UploadWriter,
        max_bytes: int,
    ) -> None:
        self._session = session
        self._service_repository = service_repository
        self._upload_repository = upload_repository
        self._storage = storage
        self._max_bytes = max_bytes

    async def create_upload(
        self,
        owner_id: int,
        service_id: int,
        *,
        content_length: int | None,
        body: AsyncIterator[bytes],
    ) -> ServiceUpload:
        """서비스 소유자만 올린다. 크기·형식이 맞지 않으면 본문을 다 받기 전에 거절한다."""
        service = await self._service_repository.find_by_id_and_owner_id(service_id, owner_id)
        if service is None:
            raise ServiceNotFoundError("service not found", service_id=service_id)
        declared_size = self._check_content_length(service_id, content_length)
        # 읽기 트랜잭션을 닫는다. 업로드를 받는 동안 DB 연결을 잡지 않는다.
        await self._session.commit()

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "upload.tar.gz"
            received = await self._receive(service_id, body, declared_size, path)
            public_id = generate_url_token()
            # 저장소에 먼저 두고 행을 남긴다. 반대 순서면 파일 없는 행이 생길 수 있다. 행 없이 남는
            # 파일은 버킷 lifecycle 이 지운다.
            storage_key = await self._storage.put_upload(public_id, path)

        now = datetime.now(UTC)
        await self._upload_repository.delete_unused_expired_before(now - EXPIRED_UPLOAD_RETENTION)
        upload = await self._upload_repository.add(
            ServiceUpload(
                public_id=public_id,
                service_id=service_id,
                uploaded_by=owner_id,
                size_bytes=received.size_bytes,
                sha256=received.sha256,
                storage_key=storage_key,
                expires_at=now + UPLOAD_TTL,
            )
        )
        await self._session.commit()
        logger.info(
            "upload stored",
            extra={
                "action": "create_upload",
                "service_id": service_id,
                "upload_id": upload.id,
                "size_bytes": received.size_bytes,
            },
        )
        return upload

    def _check_content_length(self, service_id: int, content_length: int | None) -> int:
        if content_length is None:
            raise InvalidInputError("Content-Length header is required", field="Content-Length")
        if content_length <= 0:
            raise InvalidInputError("upload body is empty", field="Content-Length")
        if content_length > self._max_bytes:
            raise UploadTooLargeError(
                "upload exceeds the size limit",
                service_id=service_id,
                content_length=content_length,
                max_bytes=self._max_bytes,
            )
        return content_length

    async def _receive(
        self, service_id: int, body: AsyncIterator[bytes], declared_size: int, path: Path
    ) -> _ReceivedArchive:
        """본문을 `path` 로 받으며 크기와 sha256 을 센다. gzip 이 아니면 첫 조각에서 끊는다."""
        digest = hashlib.sha256()
        size = 0
        head = b""
        pending = bytearray()
        with path.open("wb") as file:
            async for chunk in body:
                if len(head) < len(_GZIP_MAGIC):
                    head += chunk[: len(_GZIP_MAGIC) - len(head)]
                    if len(head) == len(_GZIP_MAGIC) and head != _GZIP_MAGIC:
                        raise UploadNotGzipError("upload is not a gzip archive")
                size += len(chunk)
                # Content-Length 보다 길게 보내는 클라이언트도 한도를 넘기지 못한다.
                if size > declared_size:
                    raise InvalidInputError("body is longer than Content-Length")
                pending += chunk
                if len(pending) >= _WRITE_BLOCK_BYTES:
                    await asyncio.to_thread(_append, file, digest.update, bytes(pending))
                    pending.clear()
            if pending:
                await asyncio.to_thread(_append, file, digest.update, bytes(pending))
        if head != _GZIP_MAGIC:
            raise UploadNotGzipError("upload is not a gzip archive")
        if size != declared_size:
            raise InvalidInputError(
                "body is shorter than Content-Length",
                service_id=service_id,
                received=size,
                content_length=declared_size,
            )
        return _ReceivedArchive(size_bytes=size, sha256=digest.hexdigest())


def _append(file: BinaryIO, update_digest: Callable[[bytes], object], data: bytes) -> None:
    update_digest(data)
    file.write(data)
