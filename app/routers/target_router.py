from fastapi import APIRouter

from app.dependencies import CurrentUserDep, TargetServiceDep
from app.schemas.response import ApiResponse, error_responses
from app.schemas.service import TargetResponse

router = APIRouter(prefix="/api/v1/targets", tags=["targets"])


@router.get(
    "",
    response_model=ApiResponse[list[TargetResponse]],
    response_model_exclude_none=True,
    summary="배포 타깃 목록",
    responses=error_responses(401),
)
async def search_targets(
    user: CurrentUserDep, service: TargetServiceDep
) -> ApiResponse[list[TargetResponse]]:
    targets = await service.search_targets()
    return ApiResponse(data=[TargetResponse.from_model(t) for t in targets])
