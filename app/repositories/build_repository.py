from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.exceptions import NotFoundError
from app.models import Build, DeploymentRequest


class BuildRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, build_id: int, *, for_update: bool = False) -> Build:
        """배포 요청·서비스를 함께 읽는다. for_update 면 세 행을 잠근다.

        잠금은 FOR NO KEY UPDATE 라 다른 트랜잭션의 FK 참조(새 배포 요청 생성)는 막지 않는다.
        """
        stmt = (
            select(Build)
            .where(Build.id == build_id)
            .options(
                joinedload(Build.deployment_request, innerjoin=True).joinedload(
                    DeploymentRequest.service, innerjoin=True
                )
            )
        )
        if for_update:
            stmt = stmt.with_for_update(key_share=True)
        build = await self._session.scalar(stmt)
        if build is None:
            raise NotFoundError("build not found", build_id=build_id)
        return build

    async def add(self, build: Build) -> Build:
        self._session.add(build)
        await self._session.flush()
        return build
