from typing import Annotated

from fastapi import APIRouter, Header, Query, status

from app.dependencies import CurrentUserDep, DeploymentHistoryServiceDep, ManualDeploymentServiceDep
from app.schemas.deployment import (
    CreateDeploymentRequest,
    DeploymentDetailResponse,
    DeploymentResponse,
)
from app.schemas.response import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    ApiResponse,
    Page,
    error_responses,
)

router = APIRouter(prefix="/api/v1", tags=["deployments"])


@router.post(
    "/services/{service_id}/deployments",
    response_model=ApiResponse[DeploymentResponse],
    response_model_exclude_none=True,
    status_code=status.HTTP_201_CREATED,
    summary="배포 요청 생성 (수동·재배포·롤백·재시작·삭제)",
    responses=error_responses(401, 404, 409, 422, 502, 503),
)
async def create_deployment_request(
    service_id: int,
    body: CreateDeploymentRequest,
    user: CurrentUserDep,
    service: ManualDeploymentServiceDep,
    idempotency_key: Annotated[
        str | None,
        Header(
            max_length=64,
            description="같은 값으로 다시 보내면 새로 만들지 않고 처음 만든 배포 요청을 돌려준다.",
        ),
    ] = None,
) -> ApiResponse[DeploymentResponse]:
    request = await service.create_deployment_request(
        user.id,
        service_id,
        trigger_type=body.trigger_type,
        source_sha=body.source_sha,
        source_deployment_request_id=body.source_deployment_id,
        idempotency_key=idempotency_key,
    )
    return ApiResponse(data=DeploymentResponse.from_model(request))


@router.get(
    "/services/{service_id}/deployments",
    response_model=ApiResponse[Page[DeploymentResponse]],
    response_model_exclude_none=True,
    summary="배포 요청 목록 (최신순)",
    responses=error_responses(401, 404, 422),
)
async def search_deployment_requests(
    service_id: int,
    user: CurrentUserDep,
    service: DeploymentHistoryServiceDep,
    page: Annotated[int, Query(ge=0, description="0부터 시작")] = 0,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> ApiResponse[Page[DeploymentResponse]]:
    result = await service.search_deployment_requests(user.id, service_id, page, size)
    return ApiResponse(
        data=Page(
            items=[DeploymentResponse.from_model(r) for r in result.items],
            total=result.total,
            page=page,
            size=size,
        )
    )


@router.get(
    "/services/{service_id}/deployments/{deployment_id}",
    response_model=ApiResponse[DeploymentDetailResponse],
    response_model_exclude_none=True,
    summary="배포 요청 상세 (상태 이력·단계별 소요 시간)",
    responses=error_responses(401, 404, 422),
)
async def get_deployment_request(
    service_id: int,
    deployment_id: int,
    user: CurrentUserDep,
    service: DeploymentHistoryServiceDep,
) -> ApiResponse[DeploymentDetailResponse]:
    detail = await service.get_deployment_request(user.id, service_id, deployment_id)
    return ApiResponse(data=DeploymentDetailResponse.from_detail(detail))
