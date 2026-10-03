from fastapi import APIRouter, Response, status

from app.dependencies import CurrentUserDep, ServiceRegistryServiceDep
from app.schemas.response import ApiResponse, error_responses
from app.schemas.service import ServiceCreateRequest, ServiceResponse, ServiceUpdateRequest

router = APIRouter(prefix="/api/v1", tags=["services"])


@router.post(
    "/projects/{project_id}/services",
    response_model=ApiResponse[ServiceResponse],
    response_model_exclude_none=True,
    status_code=status.HTTP_201_CREATED,
    summary="서비스 생성 (저장소 연결)",
    responses=error_responses(401, 403, 404, 409, 422, 502, 503),
)
async def create_service(
    project_id: int,
    body: ServiceCreateRequest,
    user: CurrentUserDep,
    service: ServiceRegistryServiceDep,
) -> ApiResponse[ServiceResponse]:
    detail = await service.create_service(
        user.id,
        project_id,
        body.repository_url,
        body.name,
        body.branch,
        body.root_directory,
        body.is_auto_deploy,
        body.target_ids,
    )
    return ApiResponse(data=ServiceResponse.from_detail(detail))


@router.get(
    "/projects/{project_id}/services",
    response_model=ApiResponse[list[ServiceResponse]],
    response_model_exclude_none=True,
    summary="서비스 목록",
    responses=error_responses(401, 404, 422, 503),
)
async def search_services(
    project_id: int, user: CurrentUserDep, service: ServiceRegistryServiceDep
) -> ApiResponse[list[ServiceResponse]]:
    details = await service.search_services(user.id, project_id)
    return ApiResponse(data=[ServiceResponse.from_detail(d) for d in details])


@router.get(
    "/services/{service_id}",
    response_model=ApiResponse[ServiceResponse],
    response_model_exclude_none=True,
    summary="서비스 조회",
    responses=error_responses(401, 404, 422, 503),
)
async def get_service(
    service_id: int, user: CurrentUserDep, service: ServiceRegistryServiceDep
) -> ApiResponse[ServiceResponse]:
    detail = await service.get_service(user.id, service_id)
    return ApiResponse(data=ServiceResponse.from_detail(detail))


@router.patch(
    "/services/{service_id}",
    response_model=ApiResponse[ServiceResponse],
    response_model_exclude_none=True,
    summary="서비스 설정 수정",
    responses=error_responses(401, 404, 409, 422, 502, 503),
)
async def update_service(
    service_id: int,
    body: ServiceUpdateRequest,
    user: CurrentUserDep,
    service: ServiceRegistryServiceDep,
) -> ApiResponse[ServiceResponse]:
    detail = await service.update_service(user.id, service_id, body.model_dump(exclude_unset=True))
    return ApiResponse(data=ServiceResponse.from_detail(detail))


@router.delete(
    "/services/{service_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="서비스 삭제 (떠 있는 앱도 함께 내린다)",
    responses=error_responses(401, 404, 409, 422, 503),
)
async def delete_service(
    service_id: int, user: CurrentUserDep, service: ServiceRegistryServiceDep
) -> Response:
    await service.delete_service(user.id, service_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
