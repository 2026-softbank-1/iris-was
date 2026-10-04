import uuid
from typing import Annotated

from fastapi import APIRouter, Header, status

from app.dependencies import (
    CurrentUserDep,
    DatabaseServiceDep,
    SourceRepositoryServiceDep,
    StackServiceDep,
)
from app.schemas.response import ApiResponse, error_responses
from app.schemas.service import ServiceResponse
from app.schemas.stack import CreateDatabaseRequest, CreateStackDeploymentRequest, StackResponse
from app.services.stack_source import branch_head_resolver

router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["stacks"])


@router.post(
    "/databases",
    response_model=ApiResponse[ServiceResponse],
    response_model_exclude_none=True,
    status_code=status.HTTP_201_CREATED,
    summary="관리형 DB 서비스 생성 (개발용 단일 인스턴스, 자동 배포 접수)",
    responses=error_responses(401, 404, 409, 422, 503),
)
async def create_database(
    project_id: int,
    body: CreateDatabaseRequest,
    user: CurrentUserDep,
    service: DatabaseServiceDep,
) -> ApiResponse[ServiceResponse]:
    detail = await service.create_database(
        user.id, project_id, body.name, body.engine, body.storage_gi, body.target_ids
    )
    return ApiResponse(data=ServiceResponse.from_detail(detail))


@router.get(
    "/stacks",
    response_model=ApiResponse[list[StackResponse]],
    response_model_exclude_none=True,
    summary="스택 목록 (서비스·배포 순서·의존 그래프·최근 상태·변경 감지)",
    responses=error_responses(401, 404, 422),
)
async def search_stacks(
    project_id: int, user: CurrentUserDep, service: StackServiceDep
) -> ApiResponse[list[StackResponse]]:
    views = await service.search_stacks(user.id, project_id)
    return ApiResponse(data=[StackResponse.from_view(v) for v in views])


@router.get(
    "/stacks/{stack_id}",
    response_model=ApiResponse[StackResponse],
    response_model_exclude_none=True,
    summary="스택 조회",
    responses=error_responses(401, 404, 422),
)
async def get_stack(
    project_id: int, stack_id: int, user: CurrentUserDep, service: StackServiceDep
) -> ApiResponse[StackResponse]:
    view = await service.get_stack(user.id, project_id, stack_id)
    return ApiResponse(data=StackResponse.from_view(view))


@router.post(
    "/stacks/{stack_id}/deployments",
    response_model=ApiResponse[StackResponse],
    response_model_exclude_none=True,
    status_code=status.HTTP_202_ACCEPTED,
    summary="스택 재배포 (DB → 의존 앱 → 나머지 순서)",
    responses=error_responses(401, 404, 409, 422, 502, 503),
)
async def create_stack_deployment(
    project_id: int,
    stack_id: int,
    body: CreateStackDeploymentRequest,
    user: CurrentUserDep,
    service: StackServiceDep,
    source_repository_service: SourceRepositoryServiceDep,
    idempotency_key: Annotated[
        str | None,
        Header(
            max_length=64,
            description="같은 값으로 다시 보내면 새로 만들지 않고 지금 스택 상태를 돌려준다.",
        ),
    ] = None,
) -> ApiResponse[StackResponse]:
    view = await service.redeploy_stack(
        user.id,
        project_id,
        stack_id,
        body.service_ids,
        branch_head_resolver(source_repository_service, user.id, service, stack_id),
        idempotency_key=idempotency_key or str(uuid.uuid4()),
        skip_variable_validation=body.skip_variable_validation,
    )
    return ApiResponse(data=StackResponse.from_view(view))
