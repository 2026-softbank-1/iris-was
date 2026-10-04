"""스택 배포의 다음 단계를 진행한다. 배포 요청이 끝난(성공·실패) 같은 트랜잭션에서 돈다.

배포 요청 상태는 `DeploymentStatusService.transition_status` 로만 바뀌므로, 거기서 끝난 요청을 넘겨
받는다. 앞 단계가 모두 SUCCEEDED 가 된 단계는 첫 job 을 만들어 시작하고, 앞 단계가 실패하면 그
단계를 보류(HELD)하고 요청을 FAILED(DEPENDENCY_FAILED)로 끝낸다. 보류된 요청의 실패가 다시 이 경로를
타서 그 뒤 단계도 차례로 보류된다.

동시에 끝난 앞 단계 둘이 서로의 결과를 못 보는 일이 없도록, 기다리는 단계 행을 잠근 뒤 새 문장으로
앞 단계 상태를 읽는다(READ COMMITTED 에서는 나중에 잠근 쪽이 먼저 커밋한 결과를 본다).
"""

import logging
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import (
    BuildStatus,
    DeploymentStatus,
    FailureCode,
    JobKind,
    StackDeploymentStepStatus,
)
from app.models.deployment_request import DeploymentRequest
from app.models.job import Job
from app.repositories.build_repository import BuildRepository
from app.repositories.job_repository import JobRepository
from app.repositories.service_stack_repository import StackDeploymentRepository
from app.schemas.job import BuildJobPayload

if TYPE_CHECKING:
    from app.services.deployment_status_service import DeploymentStatusService

logger = logging.getLogger(__name__)

_HOLDING_STATUSES = frozenset(
    {
        DeploymentStatus.FAILED,
        DeploymentStatus.ROLLED_BACK,
        DeploymentStatus.MANUAL_INTERVENTION,
        DeploymentStatus.SUPERSEDED,
    }
)


async def start_queued_request(
    session: AsyncSession, request_id: int, statuses: "DeploymentStatusService"
) -> None:
    """QUEUED 로 기다리던 요청의 첫 job 을 만든다. 이미지가 정해진(성공한 빌드) 요청은 DEPLOY 다."""
    build = await BuildRepository(session).find_by_deployment_request_id(request_id)
    assert build is not None
    jobs = JobRepository(session)
    if build.status == BuildStatus.SUCCEEDED:
        await statuses.transition_status(request_id, DeploymentStatus.DEPLOYING)
        await jobs.save(
            Job(
                deployment_request_id=request_id,
                kind=JobKind.DEPLOY,
                payload={"build_id": build.id},
            )
        )
        return
    await jobs.save(
        Job(
            deployment_request_id=request_id,
            kind=JobKind.BUILD,
            payload=BuildJobPayload(build_id=build.id).model_dump(mode="json"),
        )
    )


async def fail_before_start(
    session: AsyncSession,
    statuses: "DeploymentStatusService",
    request_id: int,
    failure_code: FailureCode,
) -> None:
    """시작하지 않은(QUEUED, job 없음) 요청을 실패로 끝낸다. 빌드는 취소로 닫는다."""
    build = await BuildRepository(session).find_by_deployment_request_id(request_id)
    if build is not None and not build.is_finished:
        build.cancel()
    step = await StackDeploymentRepository(session).find_step_by_deployment_request_id(request_id)
    if step is not None and step.status == StackDeploymentStepStatus.WAITING:
        step.status = StackDeploymentStepStatus.STARTED
    await statuses.transition_status(request_id, DeploymentStatus.FAILED, failure_code=failure_code)


class StackDeploymentProgress:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._steps = StackDeploymentRepository(session)

    async def on_request_finished(
        self, request: DeploymentRequest, statuses: "DeploymentStatusService"
    ) -> None:
        if request.status == DeploymentStatus.SUCCEEDED:
            await self._release_dependents(request, statuses)
        elif request.status in _HOLDING_STATUSES:
            await self._hold_dependents(request, statuses)

    async def _dependents(self, request: DeploymentRequest) -> list:  # type: ignore[type-arg]
        step = await self._steps.find_step_by_deployment_request_id(request.id)
        if step is None:
            return []
        return await self._steps.search_waiting_dependents_for_update(
            step.stack_deployment_id, request.id
        )

    async def _release_dependents(
        self, request: DeploymentRequest, statuses: "DeploymentStatusService"
    ) -> None:
        for dependent in await self._dependents(request):
            if dependent.status != StackDeploymentStepStatus.WAITING:
                continue
            requirements = await self._steps.search_request_statuses(
                list(dependent.depends_on_deployment_request_ids)
            )
            if any(s != DeploymentStatus.SUCCEEDED for s in requirements.values()):
                continue
            dependent.start()
            await start_queued_request(self._session, dependent.deployment_request_id, statuses)
            logger.info(
                "stack deployment step started",
                extra={
                    "action": "release_stack_step",
                    "deployment_request_id": dependent.deployment_request_id,
                    "stack_deployment_id": dependent.stack_deployment_id,
                    "step_order": dependent.step_order,
                },
            )

    async def _hold_dependents(
        self, request: DeploymentRequest, statuses: "DeploymentStatusService"
    ) -> None:
        for dependent in await self._dependents(request):
            if dependent.status != StackDeploymentStepStatus.WAITING:
                continue
            dependent.hold(request.id)
            build = await BuildRepository(self._session).find_by_deployment_request_id(
                dependent.deployment_request_id
            )
            if build is not None and not build.is_finished:
                build.cancel()
            logger.info(
                "stack deployment step held",
                extra={
                    "action": "hold_stack_step",
                    "deployment_request_id": dependent.deployment_request_id,
                    "held_by_deployment_request_id": request.id,
                },
            )
            # 이 실패가 다시 이 경로를 타 뒤 단계도 보류된다.
            await statuses.transition_status(
                dependent.deployment_request_id,
                DeploymentStatus.FAILED,
                failure_code=FailureCode.DEPENDENCY_FAILED,
            )
