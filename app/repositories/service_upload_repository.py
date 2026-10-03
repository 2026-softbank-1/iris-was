from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import UploadNotFoundError
from app.models.service_upload import ServiceUpload


class ServiceUploadRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, upload: ServiceUpload) -> ServiceUpload:
        self._session.add(upload)
        await self._session.flush()
        return upload

    async def find_by_public_id_and_service_id(
        self, public_id: str, service_id: int
    ) -> ServiceUpload | None:
        stmt = select(ServiceUpload).where(
            ServiceUpload.public_id == public_id, ServiceUpload.service_id == service_id
        )
        return (await self._session.scalars(stmt)).one_or_none()

    async def get_by_id(self, upload_id: int) -> ServiceUpload:
        upload = await self._session.get(ServiceUpload, upload_id)
        if upload is None:
            raise UploadNotFoundError("upload not found", upload_id=upload_id)
        return upload

    async def claim(self, upload_id: int, now: datetime) -> bool:
        """아직 쓰이지 않았고 만료되지 않은 업로드를 가져간다. 가져갔으면 True 다.

        조건을 UPDATE 한 문장에 담아 동시에 들어온 요청 중 하나만 이기게 한다. 진 쪽은 앞선
        트랜잭션이 끝나길 기다렸다가 조건을 다시 보고 0 행을 갱신한다. 트랜잭션을 되돌리면
        가져간 것도 되돌아간다.
        """
        stmt = (
            update(ServiceUpload)
            .where(
                ServiceUpload.id == upload_id,
                ServiceUpload.consumed_at.is_(None),
                ServiceUpload.expires_at > now,
            )
            .values(consumed_at=now)
            .returning(ServiceUpload.id)
            .execution_options(synchronize_session="fetch")
        )
        return (await self._session.execute(stmt)).scalar_one_or_none() is not None

    async def delete_unused_expired_before(self, cutoff: datetime) -> None:
        """쓰이지 못하고 만료된 지 오래된 행을 지운다. 쓰인 행은 배포 요청이 가리켜 남는다."""
        stmt = delete(ServiceUpload).where(
            ServiceUpload.consumed_at.is_(None), ServiceUpload.expires_at < cutoff
        )
        await self._session.execute(stmt)
