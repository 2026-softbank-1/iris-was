from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import distinct_on, insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import DeploymentRequestNotFoundError
from app.enums import DeploymentStatus, Environment
from app.models.deployment_request import DeploymentRequest


class DeploymentRequestRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_idempotency_key(self, idempotency_key: str) -> DeploymentRequest | None:
        stmt = select(DeploymentRequest).where(DeploymentRequest.idempotency_key == idempotency_key)
        return (await self._session.scalars(stmt)).one_or_none()

    async def find_by_id_and_service_id(
        self, deployment_request_id: int, service_id: int
    ) -> DeploymentRequest | None:
        stmt = select(DeploymentRequest).where(
            DeploymentRequest.id == deployment_request_id,
            DeploymentRequest.service_id == service_id,
        )
        return (await self._session.scalars(stmt)).one_or_none()

    async def get_by_id(self, deployment_request_id: int) -> DeploymentRequest:
        request = await self._session.scalar(
            select(DeploymentRequest).where(DeploymentRequest.id == deployment_request_id)
        )
        if request is None:
            raise DeploymentRequestNotFoundError(
                "deployment request not found", deployment_request_id=deployment_request_id
            )
        return request

    async def find_latest_succeeded_by_service_id(
        self, service_id: int
    ) -> DeploymentRequest | None:
        """서비스에서 마지막으로 성공한 배포 요청. 지금 떠 있는 버전이다."""
        stmt = (
            select(DeploymentRequest)
            .where(
                DeploymentRequest.service_id == service_id,
                DeploymentRequest.status == DeploymentStatus.SUCCEEDED,
            )
            .order_by(DeploymentRequest.created_at.desc(), DeploymentRequest.id.desc())
            .limit(1)
        )
        return (await self._session.scalars(stmt)).one_or_none()

    async def find_first_succeeded_after(
        self, service_id: int, environment: Environment, deployment_request_id: int
    ) -> DeploymentRequest | None:
        """이 요청 다음에 처음 성공한 같은 서비스·환경의 요청. 이 요청을 대신한 배포다."""
        stmt = (
            select(DeploymentRequest)
            .where(
                DeploymentRequest.service_id == service_id,
                DeploymentRequest.environment == environment,
                DeploymentRequest.status == DeploymentStatus.SUCCEEDED,
                DeploymentRequest.id > deployment_request_id,
            )
            .order_by(DeploymentRequest.id)
            .limit(1)
        )
        return (await self._session.scalars(stmt)).one_or_none()

    async def get_by_id_for_update(self, deployment_request_id: int) -> DeploymentRequest:
        """행을 잠그고 최신 값으로 읽는다. 상태 전이가 같은 요청에서 겹쳐도 차례로 처리된다."""
        stmt = (
            select(DeploymentRequest)
            .where(DeploymentRequest.id == deployment_request_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        request = (await self._session.scalars(stmt)).one_or_none()
        if request is None:
            raise DeploymentRequestNotFoundError(
                "deployment request not found", deployment_request_id=deployment_request_id
            )
        return request

    async def search_by_service_id(
        self, service_id: int, page: int, size: int
    ) -> list[DeploymentRequest]:
        """서비스의 배포 요청을 최신순으로 돌려준다. page 는 0부터 시작한다."""
        stmt = (
            select(DeploymentRequest)
            .where(DeploymentRequest.service_id == service_id)
            .order_by(DeploymentRequest.created_at.desc(), DeploymentRequest.id.desc())
            .offset(page * size)
            .limit(size)
        )
        return list((await self._session.scalars(stmt)).all())

    async def count_by_service_id(self, service_id: int) -> int:
        stmt = select(func.count()).where(DeploymentRequest.service_id == service_id)
        return (await self._session.execute(stmt)).scalar_one()

    async def search_latest_by_service_ids(
        self, service_ids: list[int]
    ) -> dict[int, DeploymentRequest]:
        """서비스별 가장 최근 배포 요청. 요청이 없는 서비스는 빠진다."""
        stmt = (
            select(DeploymentRequest)
            .where(DeploymentRequest.service_id.in_(service_ids))
            .ext(distinct_on(DeploymentRequest.service_id))
            .order_by(
                DeploymentRequest.service_id,
                DeploymentRequest.created_at.desc(),
                DeploymentRequest.id.desc(),
            )
        )
        return {r.service_id: r for r in (await self._session.scalars(stmt)).all()}

    async def add_if_absent(self, request: DeploymentRequest) -> DeploymentRequest | None:
        """멱등성 키가 겹치거나 같은 서비스·환경에 진행 중인 요청이 있으면 만들지 않고 None 이다.

        동시에 들어온 웹훅이 같은 제약을 두고 경쟁해도 트랜잭션이 깨지지 않게 DB 에 맡긴다.
        """
        stmt = (
            insert(DeploymentRequest)
            .values(
                service_id=request.service_id,
                environment=request.environment,
                source_sha=request.source_sha,
                source_commit_message=request.source_commit_message,
                trigger_type=request.trigger_type,
                idempotency_key=request.idempotency_key,
                requested_by=request.requested_by,
                variables_snapshot=request.variables_snapshot,
                scaling_snapshot=request.scaling_snapshot,
                source_deployment_request_id=request.source_deployment_request_id,
                service_upload_id=request.service_upload_id,
            )
            .on_conflict_do_nothing()
            .returning(DeploymentRequest)
        )
        return (await self._session.scalars(stmt)).one_or_none()
