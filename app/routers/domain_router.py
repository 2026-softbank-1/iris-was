from fastapi import APIRouter

from app.dependencies import CurrentUserDep, DomainServiceDep
from app.schemas.domain import ServiceDomainResponse
from app.schemas.response import ApiResponse, error_responses

router = APIRouter(prefix="/api/v1/services/{service_id}/domains", tags=["domains"])


@router.get(
    "",
    response_model=ApiResponse[list[ServiceDomainResponse]],
    response_model_exclude_none=True,
    summary="서비스 도메인 목록 (타깃별)",
    responses=error_responses(401, 404, 422),
)
async def search_domains(
    service_id: int, user: CurrentUserDep, service: DomainServiceDep
) -> ApiResponse[list[ServiceDomainResponse]]:
    details = await service.search_domains(user.id, service_id)
    return ApiResponse(data=[ServiceDomainResponse.from_detail(d) for d in details])
