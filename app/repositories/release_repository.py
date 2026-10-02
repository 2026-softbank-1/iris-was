from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.enums import ACTIVE_DEPLOYMENT_STATUSES, Environment, ReleaseStatus
from app.models.deployment_request import DeploymentRequest
from app.models.release import Release


class ReleaseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, release_id: int, *, for_update: bool = False) -> Release:
        statement = select(Release).where(Release.id == release_id)
        if for_update:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        release = (await self._session.scalars(statement)).one_or_none()
        if release is None:
            raise NotFoundError("release not found", release_id=release_id)
        return release

    async def search_by_deployment_request_id(self, deployment_request_id: int) -> list[Release]:
        statement = (
            select(Release)
            .where(Release.deployment_request_id == deployment_request_id)
            .order_by(Release.id)
        )
        return list((await self._session.scalars(statement)).all())

    async def find_last_known_good(
        self, service_id: int, environment: Environment, target_id: int
    ) -> Release | None:
        statement = (
            select(Release)
            .where(
                Release.service_id == service_id,
                Release.environment == environment,
                Release.target_id == target_id,
                Release.status == ReleaseStatus.SUCCEEDED,
            )
            .order_by(Release.created_at.desc(), Release.id.desc())
            .limit(1)
        )
        return (await self._session.scalars(statement)).one_or_none()

    async def check_newer_active_deployment(self, release: Release) -> bool:
        statement = select(DeploymentRequest.id).where(
            DeploymentRequest.service_id == release.service_id,
            DeploymentRequest.environment == release.environment,
            DeploymentRequest.id > release.deployment_request_id,
            DeploymentRequest.status.in_(ACTIVE_DEPLOYMENT_STATUSES),
        )
        return (await self._session.scalars(statement.limit(1))).one_or_none() is not None

    async def save(self, release: Release) -> Release:
        self._session.add(release)
        await self._session.flush()
        return release
