from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.exceptions import ConflictError, NotFoundError
from app.enums import ReleaseStatus
from app.models import DeploymentRequest, Release


class ReleaseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, release_id: int, *, for_update: bool = False) -> Release:
        """빌드·배포 요청·서비스를 함께 읽는다. for_update 면 읽은 행을 모두 잠근다."""
        stmt = _select_with_relations().where(Release.id == release_id)
        if for_update:
            stmt = stmt.with_for_update(key_share=True)
        release = await self._session.scalar(stmt)
        if release is None:
            raise NotFoundError("release not found", release_id=release_id)
        return release

    async def find_by_deployment_request_id(self, deployment_request_id: int) -> Release | None:
        return await self._session.scalar(
            _select_with_relations().where(Release.deployment_request_id == deployment_request_id)
        )

    async def find_last_known_good(self, service_id: int) -> Release | None:
        return await self._session.scalar(
            select(Release)
            .where(Release.service_id == service_id, Release.status == ReleaseStatus.SUCCEEDED)
            .order_by(Release.id.desc())
            .limit(1)
        )

    async def add(self, release: Release) -> Release:
        """진행 중 release 가 이미 있으면 ConflictError. 그 뒤 이 트랜잭션은 rollback 해야 한다."""
        self._session.add(release)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise ConflictError("release in flight", service_id=release.service_id) from exc
        return release


def _select_with_relations() -> Select[Release]:
    return select(Release).options(
        joinedload(Release.build, innerjoin=True),
        joinedload(Release.deployment_request, innerjoin=True).joinedload(
            DeploymentRequest.service, innerjoin=True
        ),
    )
