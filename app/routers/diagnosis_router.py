from fastapi import APIRouter, status

from app.dependencies import CurrentUserDep, DiagnosisServiceDep
from app.schemas.diagnosis import DiagnosisResponse
from app.schemas.response import ApiResponse, error_responses

router = APIRouter(
    prefix="/api/v1/services/{service_id}/deployments/{deployment_id}/diagnosis", tags=["diagnosis"]
)


@router.post(
    "",
    response_model=ApiResponse[DiagnosisResponse],
    status_code=status.HTTP_202_ACCEPTED,
    summary="실패 배포 로그 진단 접수",
    responses=error_responses(401, 404, 409, 422, 503),
)
async def create_diagnosis(
    service_id: int, deployment_id: int, user: CurrentUserDep, service: DiagnosisServiceDep
) -> ApiResponse[DiagnosisResponse]:
    row = await service.create_diagnosis(user.id, service_id, deployment_id)
    return ApiResponse(data=DiagnosisResponse.from_model(row))


@router.get(
    "",
    response_model=ApiResponse[DiagnosisResponse],
    summary="최근 실패 로그 진단 조회",
    responses=error_responses(401, 404, 422),
)
async def get_diagnosis(
    service_id: int, deployment_id: int, user: CurrentUserDep, service: DiagnosisServiceDep
) -> ApiResponse[DiagnosisResponse]:
    row = await service.get_latest_diagnosis(user.id, service_id, deployment_id)
    return ApiResponse(data=DiagnosisResponse.from_model(row))
