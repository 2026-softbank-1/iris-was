from fastapi import APIRouter, status

from app.dependencies import AnalysisServiceDep, CurrentUserDep
from app.schemas.analysis import (
    AnalysisResponse,
    CancelAnalysisRequest,
    ConfirmAnalysisRequest,
    CreateAnalysisRequest,
)
from app.schemas.response import ApiResponse, error_responses

router = APIRouter(prefix="/api/v1/services/{service_id}/analysis", tags=["analysis"])


@router.post(
    "",
    response_model=ApiResponse[AnalysisResponse],
    response_model_exclude_none=True,
    status_code=status.HTTP_202_ACCEPTED,
    summary="고정 SHA 소스 분석 접수",
    responses=error_responses(401, 403, 404, 409, 422, 502, 503),
)
async def create_analysis(
    service_id: int, body: CreateAnalysisRequest, user: CurrentUserDep, service: AnalysisServiceDep
) -> ApiResponse[AnalysisResponse]:
    analysis = await service.create_analysis(user.id, service_id, body.mode)
    return ApiResponse(data=AnalysisResponse.from_model(analysis))


@router.get(
    "",
    response_model=ApiResponse[AnalysisResponse],
    response_model_exclude_none=True,
    summary="최근 소스 분석 결과 조회",
    responses=error_responses(401, 404, 422),
)
async def get_latest_analysis(
    service_id: int, user: CurrentUserDep, service: AnalysisServiceDep
) -> ApiResponse[AnalysisResponse]:
    analysis = await service.get_latest_analysis(user.id, service_id)
    return ApiResponse(data=AnalysisResponse.from_model(analysis))


@router.post(
    "/cancel",
    response_model=ApiResponse[AnalysisResponse],
    response_model_exclude_none=True,
    summary="진행 중 소스 분석 취소",
    responses=error_responses(401, 404, 409, 422),
)
async def cancel_analysis(
    service_id: int, body: CancelAnalysisRequest, user: CurrentUserDep, service: AnalysisServiceDep
) -> ApiResponse[AnalysisResponse]:
    analysis = await service.cancel_analysis(user.id, service_id, body.analysis_id)
    return ApiResponse(data=AnalysisResponse.from_model(analysis))


@router.post(
    "/answers",
    response_model=ApiResponse[AnalysisResponse],
    response_model_exclude_none=True,
    summary="분석 후보와 실행 설정 명시 확인",
    responses=error_responses(401, 403, 404, 409, 422, 502, 503),
)
async def confirm_analysis(
    service_id: int, body: ConfirmAnalysisRequest, user: CurrentUserDep, service: AnalysisServiceDep
) -> ApiResponse[AnalysisResponse]:
    analysis = await service.confirm_analysis(user.id, service_id, body)
    return ApiResponse(data=AnalysisResponse.from_model(analysis))
