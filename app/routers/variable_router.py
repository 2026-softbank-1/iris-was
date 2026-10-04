from fastapi import APIRouter, Response, status

from app.dependencies import CurrentUserDep, VariableServiceDep, VariableValidationServiceDep
from app.schemas.response import ApiResponse, error_responses
from app.schemas.variable import (
    ServiceVariablesResponse,
    VariableCreateRequest,
    VariableResponse,
    VariablesRawRequest,
    VariablesValidationResponse,
    VariableUpdateRequest,
)

router = APIRouter(prefix="/api/v1/services/{service_id}/variables", tags=["variables"])


@router.get(
    "",
    response_model=ApiResponse[ServiceVariablesResponse],
    response_model_exclude_none=True,
    summary="환경변수 목록 (자동 주입 변수 포함)",
    responses=error_responses(401, 404, 422, 503),
)
async def search_variables(
    service_id: int, user: CurrentUserDep, service: VariableServiceDep
) -> ApiResponse[ServiceVariablesResponse]:
    result = await service.search_variables(user.id, service_id)
    return ApiResponse(data=ServiceVariablesResponse.from_service_variables(result))


@router.post(
    "",
    response_model=ApiResponse[VariableResponse],
    response_model_exclude_none=True,
    status_code=status.HTTP_201_CREATED,
    summary="환경변수 추가 (값 또는 다른 서비스 연결 정보 참조)",
    responses=error_responses(401, 404, 409, 422, 503),
)
async def create_variable(
    service_id: int,
    body: VariableCreateRequest,
    user: CurrentUserDep,
    service: VariableServiceDep,
) -> ApiResponse[VariableResponse]:
    entry = await service.create_variable(
        user.id,
        service_id,
        body.key,
        body.value,
        body.reference.to_reference() if body.reference is not None else None,
    )
    return ApiResponse(data=VariableResponse.from_entry(entry))


@router.put(
    "",
    response_model=ApiResponse[ServiceVariablesResponse],
    response_model_exclude_none=True,
    summary="환경변수 Raw(.env) 일괄 저장 (전체 교체)",
    responses=error_responses(401, 404, 422, 503),
)
async def replace_variables(
    service_id: int,
    body: VariablesRawRequest,
    user: CurrentUserDep,
    service: VariableServiceDep,
) -> ApiResponse[ServiceVariablesResponse]:
    result = await service.replace_variables(user.id, service_id, body.raw)
    return ApiResponse(data=ServiceVariablesResponse.from_service_variables(result))


@router.get(
    "/validation",
    response_model=ApiResponse[VariablesValidationResponse],
    response_model_exclude_none=True,
    summary="배포 전 환경변수 검증 (필수 키·localhost·호스트·스킴·참조)",
    responses=error_responses(401, 404, 422, 503),
)
async def validate_variables(
    service_id: int, user: CurrentUserDep, service: VariableValidationServiceDep
) -> ApiResponse[VariablesValidationResponse]:
    result = await service.validate_owned(user.id, service_id)
    return ApiResponse(data=VariablesValidationResponse.from_validation(result))


@router.put(
    "/{key}",
    response_model=ApiResponse[VariableResponse],
    response_model_exclude_none=True,
    summary="환경변수 값·참조 수정",
    responses=error_responses(401, 404, 422, 503),
)
async def update_variable(
    service_id: int,
    key: str,
    body: VariableUpdateRequest,
    user: CurrentUserDep,
    service: VariableServiceDep,
) -> ApiResponse[VariableResponse]:
    entry = await service.update_variable(
        user.id,
        service_id,
        key,
        body.value,
        body.reference.to_reference() if body.reference is not None else None,
    )
    return ApiResponse(data=VariableResponse.from_entry(entry))


@router.delete(
    "/{key}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="환경변수 삭제",
    responses=error_responses(401, 404, 422, 503),
)
async def delete_variable(
    service_id: int, key: str, user: CurrentUserDep, service: VariableServiceDep
) -> Response:
    await service.delete_variable(user.id, service_id, key)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
