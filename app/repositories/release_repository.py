from sqlalchemy import ColumnElement, Select, exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from app.core.exceptions import ConflictError, NotFoundError
from app.enums import DeploymentStatus, DeploymentTrigger, ReleaseStatus, TargetKind
from app.models import DeploymentRequest, Release, ServiceTarget, Target


def removed_after_release() -> ColumnElement[bool]:
    """이 release 를 만든 요청보다 나중에 서비스를 내린(성공한 REMOVE) 요청이 있다.

    내려간 서비스에는 정상 release 가 없다. lastKnownGood·online 판단에서 이런 release 를 뺀다.
    """
    return exists().where(
        DeploymentRequest.service_id == Release.service_id,
        DeploymentRequest.trigger_type == DeploymentTrigger.REMOVE,
        DeploymentRequest.status == DeploymentStatus.SUCCEEDED,
        DeploymentRequest.id > Release.deployment_request_id,
    )


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

    async def find_last_known_good(self, service_id: int, target_id: int) -> Release | None:
        return await self._session.scalar(
            select(Release)
            .where(
                Release.service_id == service_id,
                Release.target_id == target_id,
                Release.status == ReleaseStatus.SUCCEEDED,
                ~removed_after_release(),
            )
            .order_by(Release.id.desc())
            .limit(1)
        )

    async def find_deploy_target(self, service_id: int) -> Target | None:
        """서비스가 배포되는 AWS 타깃. 지정이 없으면 기본 `aws` 타깃을 쓴다."""
        # ponytail: 서비스당 AWS 타깃 하나만 지원한다(GitOps 경로·Argo Application 이 하나다).
        #   다중 타깃은 타깃별 GitOps 경로가 정해지면 release 를 타깃마다 만든다.
        target = await self._session.scalar(
            select(Target)
            .join(ServiceTarget, ServiceTarget.target_id == Target.id)
            .where(ServiceTarget.service_id == service_id, Target.kind == TargetKind.AWS)
            .order_by(Target.id)
            .limit(1)
        )
        if target is not None:
            return target
        return await self._session.scalar(select(Target).where(Target.name == "aws"))

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
        # 모든 서비스가 공유하는 타깃 행을 for_update 로 잠그지 않도록 따로 읽는다.
        selectinload(Release.target),
        joinedload(Release.deployment_request, innerjoin=True).joinedload(
            DeploymentRequest.service, innerjoin=True
        ),
    )
