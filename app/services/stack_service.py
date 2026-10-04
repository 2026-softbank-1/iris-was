"""스택(같은 레포 분석에서 만든 서비스 묶음)의 의존 순서 배포와 조회.

의존 그래프: 서비스 A 가 B 를 쓰면 A → B 다. 근거는 A 의 호스트 별칭 대상, A 의 참조 변수 대상,
분석기
unit 의 dependsOn(같은 스택의 unit·dependency id) 셋이다. 순서(order)는 그래프 깊이 + 1 이라 DB 는
1,
DB 를 쓰는 앱(api·worker)은 2, 그 앱을 쓰는 앱(web)은 3 이 된다.

스택 배포는 서비스마다 기존 경로로 배포 요청을 만든다. 같은 배포 안의 앞 단계가 없는 요청만 바로
job 을 만들고, 나머지는 QUEUED 로 두었다가 앞 단계가 모두 SUCCEEDED 가 되면
`StackDeploymentProgress`
가 시작한다. 앞 단계가 실패하면 뒤 단계는 보류(FAILED · DEPENDENCY_FAILED)된다.
"""

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    DeploymentInProgressError,
    FieldIssue,
    InvalidInputError,
    ProjectNotFoundError,
    StackNotFoundError,
    TargetNotConnectedError,
)
from app.enums import (
    ACTIVE_DEPLOYMENT_STATUSES,
    DatabaseEngine,
    DeploymentStatus,
    DeploymentTrigger,
    FailureCode,
    ServiceKind,
    StackDeploymentStepStatus,
)
from app.models.deployment_request import DeploymentRequest
from app.models.service import Service
from app.models.service_stack import ServiceStack, StackDeployment, StackDeploymentStep
from app.repositories.deployment_request_repository import DeploymentRequestRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.service_repository import ServiceRepository
from app.repositories.service_stack_repository import (
    ServiceStackRepository,
    StackDeploymentRepository,
)
from app.repositories.service_variable_repository import ServiceVariableRepository
from app.services.database_engines import resolve_image
from app.services.deployment_request_service import DeploymentRequestService
from app.services.deployment_status_service import DeploymentStatusService
from app.services.stack_progress import fail_before_start
from app.services.variable_validation import VariableValidationService

logger = logging.getLogger(__name__)

# 앱의 소스 커밋을 정한다. (커밋 SHA, 메시지). 스택 하나는 저장소·브랜치가 같아 한 번만 부른다.
SourceResolver = Callable[[], Awaitable[tuple[str, str | None]]]


@dataclass(frozen=True)
class StackGraph:
    """서비스 id → 같은 스택에서 쓰는 서비스 id, 그리고 순서(1부터)."""

    depends_on: dict[int, set[int]]
    order: dict[int, int]

    def topological(self, service_ids: list[int]) -> list[int]:
        return sorted(service_ids, key=lambda sid: (self.order.get(sid, 1), sid))


@dataclass(frozen=True)
class StackServiceView:
    service: Service
    order: int
    depends_on_unit_ids: list[str]
    status: str
    deployment_request: DeploymentRequest | None = None
    held_by_unit_id: str | None = None
    waiting_for_unit_ids: list[str] = field(default_factory=list)
    failure_code: FailureCode | None = None


@dataclass(frozen=True)
class StackView:
    stack: ServiceStack
    services: list[StackServiceView]
    latest_deployment: StackDeployment | None
    is_deploying: bool


@dataclass(frozen=True)
class CreatedStackDeployment:
    stack_deployment: StackDeployment
    requests: dict[int, DeploymentRequest]
    skipped_service_ids: list[int]


async def build_stack_graph(
    services: list[Service], variable_repository: ServiceVariableRepository
) -> StackGraph:
    by_id = {s.id: s for s in services}
    by_unit = {s.stack_unit_id: s.id for s in services if s.stack_unit_id}
    depends_on: dict[int, set[int]] = {s.id: set() for s in services}
    for service in services:
        edges = depends_on[service.id]
        for alias in service.host_aliases or []:
            target = alias.get("targetServiceId")
            if isinstance(target, int) and target in by_id:
                edges.add(target)
        for variable in await variable_repository.search_by_service_id(service.id):
            target = (variable.reference or {}).get("serviceId")
            if isinstance(target, int) and target in by_id:
                edges.add(target)
        unit = (service.analysis_plan or {}).get("unit")
        if isinstance(unit, dict):
            for unit_id in unit.get("dependsOn") or []:
                target = by_unit.get(unit_id)
                if target is not None:
                    edges.add(target)
        edges.discard(service.id)
    return StackGraph(depends_on, _orders(depends_on))


def _orders(depends_on: Mapping[int, set[int]]) -> dict[int, int]:
    """그래프 깊이 + 1. 순환은 돌아오는 간선을 무시한다(compose 에서 순환은 드물다)."""
    order: dict[int, int] = {}
    visiting: set[int] = set()

    def visit(node: int) -> int:
        if node in order:
            return order[node]
        visiting.add(node)
        depth = 1
        for dependency in sorted(depends_on.get(node, ())):
            if dependency in visiting:
                continue
            depth = max(depth, visit(dependency) + 1)
        visiting.discard(node)
        order[node] = depth
        return depth

    for node in sorted(depends_on):
        visit(node)
    return order


class StackService:
    def __init__(
        self,
        session: AsyncSession,
        project_repository: ProjectRepository,
        service_repository: ServiceRepository,
        service_variable_repository: ServiceVariableRepository,
        stack_repository: ServiceStackRepository,
        stack_deployment_repository: StackDeploymentRepository,
        deployment_request_repository: DeploymentRequestRepository,
        deployment_request_service: DeploymentRequestService,
        variable_validation_service: VariableValidationService,
        *,
        database_images: Mapping[str, str] | None = None,
    ) -> None:
        self._session = session
        self._project_repository = project_repository
        self._service_repository = service_repository
        self._service_variable_repository = service_variable_repository
        self._stack_repository = stack_repository
        self._stack_deployment_repository = stack_deployment_repository
        self._deployment_request_repository = deployment_request_repository
        self._deployment_request_service = deployment_request_service
        self._variable_validation_service = variable_validation_service
        self._database_images = dict(database_images or {})

    # --- 조회

    async def search_stacks(self, owner_id: int, project_id: int) -> list[StackView]:
        await self._get_project(owner_id, project_id)
        views = []
        for stack in await self._stack_repository.search_by_project_id(project_id):
            view = await self._view(stack)
            if view.services:
                views.append(view)
        return views

    async def get_stack(self, owner_id: int, project_id: int, stack_id: int) -> StackView:
        stack = await self._get_stack(owner_id, project_id, stack_id)
        return await self._view(stack)

    # --- 배포

    async def redeploy_stack(
        self,
        owner_id: int,
        project_id: int,
        stack_id: int,
        service_ids: list[int] | None,
        source: SourceResolver,
        *,
        idempotency_key: str,
        skip_variable_validation: bool = False,
    ) -> StackView:
        """스택 전체(또는 `service_ids`)를 의존 순서대로 다시 배포한다.

        전체 재배포에서 이미 떠 있는 관리형 DB 는 다시 띄우지 않는다(데이터베이스를 재시작하지
        않는다). `service_ids` 로 직접 고르면 DB 도 다시 배포한다. 한 서비스라도 진행 중인 배포가
        있으면 아무것도 만들지 않고 409 다. 환경변수 error 가 있으면 422 VARIABLES_INVALID 다.
        """
        stack = await self._get_stack(owner_id, project_id, stack_id)
        key = f"stack:{stack.id}:{idempotency_key}"
        replayed = await self._stack_deployment_repository.find_by_idempotency_key(key)
        if replayed is not None:
            return await self._view(stack)
        services = await self._service_repository.search_by_stack_id(stack.id)
        if service_ids is not None:
            unknown = set(service_ids) - {s.id for s in services}
            if unknown or not service_ids:
                raise InvalidInputError(
                    "services must belong to the stack",
                    issues=[FieldIssue("serviceIds", "not_in_stack")],
                    field="serviceIds",
                    stack_id=stack.id,
                )
            selected = [s for s in services if s.id in set(service_ids)]
        else:
            selected = [s for s in services if not await self.is_live_database(s)]
        if not selected:
            raise InvalidInputError(
                "nothing to deploy in the stack",
                issues=[FieldIssue("serviceIds", "nothing_to_deploy")],
                field="serviceIds",
                stack_id=stack.id,
            )
        if not skip_variable_validation:
            await self._variable_validation_service.check_deployable(selected)
        created = await self.create_stack_deployment(
            stack,
            selected,
            trigger_type=DeploymentTrigger.MANUAL,
            idempotency_key=key,
            requested_by=owner_id,
            source=source,
            is_strict=True,
        )
        await self._session.commit()
        logger.info(
            "stack deployment requested",
            extra={
                "action": "redeploy_stack",
                "stack_id": stack.id,
                "stack_deployment_id": created.stack_deployment.id,
                "service_ids": sorted(created.requests),
            },
        )
        return await self._view(stack)

    async def create_stack_deployment(
        self,
        stack: ServiceStack,
        services: list[Service],
        *,
        trigger_type: DeploymentTrigger,
        idempotency_key: str,
        requested_by: int | None,
        source: SourceResolver,
        is_strict: bool,
        all_services: list[Service] | None = None,
        blocked_service_ids: frozenset[int] = frozenset(),
    ) -> CreatedStackDeployment:
        """`services` 마다 배포 요청을 만들고 앞 단계를 기다리게 묶는다. 커밋하지 않는다.

        `is_strict` 면 진행 중 배포가 있는 서비스가 있을 때 DeploymentInProgressError(호출한 쪽이
        롤백), 아니면 그 서비스를 건너뛰고(뒤 단계는 그 서비스를 기다리지 않는다) 나머지를 만든다.
        `blocked_service_ids` 는 환경변수 검증에서 막힌 서비스다. 요청을 만들되 시작하지 않고
        FAILED(VARIABLES_INVALID)로 끝내, 그 서비스를 기다리는 뒤 단계도 보류된다(푸시 자동 배포).
        """
        replayed = await self._stack_deployment_repository.find_by_idempotency_key(idempotency_key)
        if replayed is not None:
            return CreatedStackDeployment(replayed, {}, [])
        stack_services = all_services or await self._service_repository.search_by_stack_id(stack.id)
        graph = await build_stack_graph(stack_services, self._service_variable_repository)
        selected_ids = {s.id for s in services}
        by_id = {s.id: s for s in services}
        stack_deployment = await self._stack_deployment_repository.save(
            StackDeployment(
                stack_id=stack.id,
                trigger_type=trigger_type,
                idempotency_key=idempotency_key,
                requested_by=requested_by,
            )
        )
        resolved_source: tuple[str, str | None] | None = None
        requests: dict[int, DeploymentRequest] = {}
        skipped: list[int] = []
        for service_id in graph.topological(list(selected_ids)):
            service = by_id[service_id]
            waits_for = [
                requests[d].id
                for d in sorted(graph.depends_on.get(service_id, ()))
                if d in requests
            ]
            is_blocked = service.id in blocked_service_ids
            is_started = not waits_for and not is_blocked
            request_key = f"{idempotency_key}:{service.id}"
            try:
                if service.kind == ServiceKind.DATABASE:
                    assert service.database_engine is not None
                    request = (
                        await self._deployment_request_service.create_database_deployment_request(
                            service,
                            image=resolve_image(
                                DatabaseEngine(service.database_engine), self._database_images
                            ),
                            trigger_type=trigger_type,
                            idempotency_key=request_key,
                            requested_by=requested_by,
                            is_started=is_started,
                        )
                    )
                else:
                    if resolved_source is None:
                        resolved_source = await source()
                    request = await self._deployment_request_service.create_deployment_request(
                        service,
                        source_sha=resolved_source[0],
                        source_commit_message=resolved_source[1],
                        trigger_type=trigger_type,
                        idempotency_key=request_key,
                        requested_by=requested_by,
                        is_started=is_started,
                    )
            except TargetNotConnectedError:
                # 연결되지 않은 등록 서버로는 배포하지 않는다. 푸시는 그 서비스만 건너뛴다.
                if is_strict:
                    raise
                request = None
            if request is None:
                if is_strict:
                    raise DeploymentInProgressError(
                        "a deployment is already in progress", service_id=service.id
                    )
                skipped.append(service.id)
                continue
            requests[service.id] = request
            await self._stack_deployment_repository.add_step(
                StackDeploymentStep(
                    stack_deployment_id=stack_deployment.id,
                    service_id=service.id,
                    deployment_request_id=request.id,
                    step_order=graph.order.get(service.id, 1),
                    depends_on_deployment_request_ids=waits_for,
                    status=(
                        StackDeploymentStepStatus.STARTED
                        if is_started
                        else StackDeploymentStepStatus.WAITING
                    ),
                )
            )
        statuses = DeploymentStatusService.create(self._session)
        for service_id in graph.topological(list(blocked_service_ids & set(requests))):
            await fail_before_start(
                self._session, statuses, requests[service_id].id, FailureCode.VARIABLES_INVALID
            )
        return CreatedStackDeployment(stack_deployment, requests, skipped)

    async def get_stack_model(self, stack_id: int) -> ServiceStack:
        return await self._stack_repository.get_by_id(stack_id)

    # --- 내부

    async def is_live_database(self, service: Service) -> bool:
        if service.kind != ServiceKind.DATABASE:
            return False
        live = await self._deployment_request_repository.find_latest_succeeded_by_service_id(
            service.id
        )
        return live is not None and live.trigger_type != DeploymentTrigger.REMOVE

    async def _view(self, stack: ServiceStack) -> StackView:
        services = await self._service_repository.search_by_stack_id(stack.id)
        graph = await build_stack_graph(services, self._service_variable_repository)
        unit_by_service = {s.id: s.stack_unit_id or str(s.id) for s in services}
        latest = await self._deployment_request_repository.search_latest_by_service_ids(
            [s.id for s in services]
        )
        stack_deployment = await self._stack_deployment_repository.find_latest_by_stack_id(stack.id)
        steps: dict[int, StackDeploymentStep] = {}
        if stack_deployment is not None:
            steps = {
                step.service_id: step
                for step in await self._stack_deployment_repository.search_steps(
                    stack_deployment.id
                )
            }
        unit_by_request: dict[int, str] = {
            step.deployment_request_id: unit_by_service.get(step.service_id, "")
            for step in steps.values()
        }
        views: list[StackServiceView] = []
        is_deploying = False
        for service_id in graph.topological([s.id for s in services]):
            service = next(s for s in services if s.id == service_id)
            request = latest.get(service.id)
            step = steps.get(service.id)
            status = request.status.value if request is not None else "NOT_DEPLOYED"
            held_by = None
            waiting_for: list[str] = []
            if (
                step is not None
                and request is not None
                and step.deployment_request_id == request.id
            ):
                if step.status == StackDeploymentStepStatus.HELD:
                    status = "HELD"
                    held_by = unit_by_request.get(step.held_by_deployment_request_id or 0)
                elif step.status == StackDeploymentStepStatus.WAITING:
                    statuses = await self._stack_deployment_repository.search_request_statuses(
                        list(step.depends_on_deployment_request_ids)
                    )
                    waiting_for = [
                        unit_by_request.get(rid, str(rid))
                        for rid, s in statuses.items()
                        if s != DeploymentStatus.SUCCEEDED
                    ]
            if request is not None and request.status in ACTIVE_DEPLOYMENT_STATUSES:
                is_deploying = True
            views.append(
                StackServiceView(
                    service=service,
                    order=graph.order.get(service.id, 1),
                    depends_on_unit_ids=sorted(
                        unit_by_service[d] for d in graph.depends_on.get(service.id, ())
                    ),
                    status=status,
                    deployment_request=request,
                    held_by_unit_id=held_by,
                    waiting_for_unit_ids=sorted(waiting_for),
                    failure_code=request.failure_code if request is not None else None,
                )
            )
        return StackView(stack, views, stack_deployment, is_deploying)

    async def _get_project(self, owner_id: int, project_id: int) -> Any:
        project = await self._project_repository.find_by_id_and_owner_id(project_id, owner_id)
        if project is None:
            raise ProjectNotFoundError("project not found", project_id=project_id)
        return project

    async def _get_stack(self, owner_id: int, project_id: int, stack_id: int) -> ServiceStack:
        await self._get_project(owner_id, project_id)
        stack = await self._stack_repository.find_by_id_and_project_id(stack_id, project_id)
        if stack is None:
            raise StackNotFoundError("stack not found", stack_id=stack_id)
        return stack
