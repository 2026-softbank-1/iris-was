from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.deployment_request import DeploymentRequest
from app.models.project import Project
from app.models.service_stack import ServiceStack, StackDeployment, StackDeploymentStep


class ServiceStackRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, stack: ServiceStack) -> ServiceStack:
        self._session.add(stack)
        await self._session.flush()
        return stack

    async def get_by_id(self, stack_id: int, *, for_update: bool = False) -> ServiceStack:
        stmt = select(ServiceStack).where(ServiceStack.id == stack_id)
        if for_update:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return (await self._session.scalars(stmt)).one()

    async def find_by_id_and_project_id(
        self, stack_id: int, project_id: int, *, for_update: bool = False
    ) -> ServiceStack | None:
        stmt = select(ServiceStack).where(
            ServiceStack.id == stack_id, ServiceStack.project_id == project_id
        )
        if for_update:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return (await self._session.scalars(stmt)).one_or_none()

    async def find_by_source(
        self, project_id: int, repository_url: str, branch: str, root_directory: str | None
    ) -> ServiceStack | None:
        """같은 프로젝트·저장소·브랜치·위치의 스택(증분 apply 가 이어 붙는 곳)."""
        stmt = (
            select(ServiceStack)
            .where(
                ServiceStack.project_id == project_id,
                func.lower(ServiceStack.source_repository_url) == repository_url.lower(),
                ServiceStack.source_branch == branch,
                (
                    ServiceStack.root_directory.is_(None)
                    if root_directory is None
                    else ServiceStack.root_directory == root_directory
                ),
            )
            .order_by(ServiceStack.id)
            .limit(1)
            .with_for_update()
        )
        return (await self._session.scalars(stmt)).one_or_none()

    async def search_by_project_id(self, project_id: int) -> list[ServiceStack]:
        stmt = (
            select(ServiceStack)
            .where(ServiceStack.project_id == project_id)
            .order_by(ServiceStack.id)
        )
        return list((await self._session.scalars(stmt)).all())

    async def search_by_repository(self, repository_url: str, branch: str) -> list[ServiceStack]:
        """푸시가 온 저장소·브랜치의 스택. 지운 프로젝트의 스택은 뺀다."""
        stmt = (
            select(ServiceStack)
            .join(Project, Project.id == ServiceStack.project_id)
            .where(
                func.lower(ServiceStack.source_repository_url) == repository_url.lower(),
                ServiceStack.source_branch == branch,
                Project.is_deleted.is_(False),
            )
            .order_by(ServiceStack.id)
        )
        return list((await self._session.scalars(stmt)).all())


class StackDeploymentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, deployment: StackDeployment) -> StackDeployment:
        self._session.add(deployment)
        await self._session.flush()
        return deployment

    async def add_step(self, step: StackDeploymentStep) -> StackDeploymentStep:
        self._session.add(step)
        await self._session.flush()
        return step

    async def find_by_idempotency_key(self, idempotency_key: str) -> StackDeployment | None:
        stmt = select(StackDeployment).where(StackDeployment.idempotency_key == idempotency_key)
        return (await self._session.scalars(stmt)).one_or_none()

    async def find_latest_by_stack_id(self, stack_id: int) -> StackDeployment | None:
        stmt = (
            select(StackDeployment)
            .where(StackDeployment.stack_id == stack_id)
            .order_by(StackDeployment.id.desc())
            .limit(1)
        )
        return (await self._session.scalars(stmt)).one_or_none()

    async def search_steps(self, stack_deployment_id: int) -> list[StackDeploymentStep]:
        stmt = (
            select(StackDeploymentStep)
            .where(StackDeploymentStep.stack_deployment_id == stack_deployment_id)
            .order_by(StackDeploymentStep.step_order, StackDeploymentStep.id)
        )
        return list((await self._session.scalars(stmt)).all())

    async def find_step_by_deployment_request_id(
        self, deployment_request_id: int
    ) -> StackDeploymentStep | None:
        stmt = select(StackDeploymentStep).where(
            StackDeploymentStep.deployment_request_id == deployment_request_id
        )
        return (await self._session.scalars(stmt)).one_or_none()

    async def search_waiting_dependents_for_update(
        self, stack_deployment_id: int, deployment_request_id: int
    ) -> list[StackDeploymentStep]:
        """이 요청을 기다리는 같은 스택 배포의 단계. 동시에 끝난 앞 단계끼리 겹치지 않게 잠근다."""
        stmt = (
            select(StackDeploymentStep)
            .where(
                StackDeploymentStep.stack_deployment_id == stack_deployment_id,
                StackDeploymentStep.depends_on_deployment_request_ids.contains(
                    [deployment_request_id]
                ),
            )
            .order_by(StackDeploymentStep.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return list((await self._session.scalars(stmt)).all())

    async def search_request_statuses(self, deployment_request_ids: list[int]) -> dict[int, str]:
        """잠금을 잡은 뒤 새 문장으로 읽어 다른 트랜잭션이 커밋한 상태까지 본다."""
        if not deployment_request_ids:
            return {}
        stmt = select(DeploymentRequest.id, DeploymentRequest.status).where(
            DeploymentRequest.id.in_(deployment_request_ids)
        )
        return {row[0]: row[1] for row in (await self._session.execute(stmt)).all()}
