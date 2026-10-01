from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.deployment_request import DeploymentRequest


class DeploymentRequestRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_idempotency_key(self, idempotency_key: str) -> DeploymentRequest | None:
        stmt = select(DeploymentRequest).where(DeploymentRequest.idempotency_key == idempotency_key)
        return (await self._session.scalars(stmt)).one_or_none()

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
            )
            .on_conflict_do_nothing()
            .returning(DeploymentRequest)
        )
        return (await self._session.scalars(stmt)).one_or_none()
