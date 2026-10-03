"""업로드 API·CLI 배포 요청 테스트용 가짜 Repository·저장소."""

import asyncio
from datetime import datetime
from itertools import count
from pathlib import Path

from app.core.exceptions import ExternalError, NotFoundError, UploadNotFoundError
from app.models.base import now_utc
from app.models.service_upload import ServiceUpload
from tests.fakes import FakeSession


class FakeServiceUploadRepository:
    def __init__(self, session: FakeSession) -> None:
        self.uploads: list[ServiceUpload] = []
        self._session = session
        self._ids = count(1)

    async def add(self, upload: ServiceUpload) -> ServiceUpload:
        upload.id = next(self._ids)
        upload.created_at = upload.updated_at = now_utc()
        self.uploads.append(upload)
        return upload

    async def find_by_public_id_and_service_id(
        self, public_id: str, service_id: int
    ) -> ServiceUpload | None:
        return next(
            (u for u in self.uploads if u.public_id == public_id and u.service_id == service_id),
            None,
        )

    async def get_by_id(self, upload_id: int) -> ServiceUpload:
        upload = next((u for u in self.uploads if u.id == upload_id), None)
        if upload is None:
            raise UploadNotFoundError("upload not found", upload_id=upload_id)
        return upload

    async def claim(self, upload_id: int, now: datetime) -> bool:
        upload = await self.get_by_id(upload_id)
        if upload.consumed_at is not None or upload.expires_at <= now:
            return False
        upload.consumed_at = now
        # 실제 DB 에서는 트랜잭션을 되돌리면 가져간 것도 되돌아간다.
        self._session.on_rollback(lambda: setattr(upload, "consumed_at", None))
        return True

    async def delete_unused_expired_before(self, cutoff: datetime) -> None:
        self.uploads = [
            u for u in self.uploads if u.consumed_at is not None or u.expires_at >= cutoff
        ]


class FakeUploadStorage:
    """UploadWriter·UploadReader 를 함께 만족하는 메모리 저장소."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.fail_with: Exception | None = None
        self.download_error: Exception | None = None
        self.downloads: list[str] = []

    async def put_upload(self, public_id: str, path: Path) -> str:
        if self.fail_with is not None:
            raise self.fail_with
        key = f"uploads/{public_id}.tar.gz"
        self.objects[key] = await asyncio.to_thread(path.read_bytes)
        return key

    async def download_upload(self, storage_key: str, dest: Path) -> None:
        self.downloads.append(storage_key)
        if self.download_error is not None:
            raise self.download_error
        if storage_key not in self.objects:
            raise NotFoundError("upload object not found")
        await asyncio.to_thread(dest.write_bytes, self.objects[storage_key])


def storage_failure() -> ExternalError:
    return ExternalError("aws request failed", operation="upload_file")
