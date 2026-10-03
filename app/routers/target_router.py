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
    """공용 타깃(`aws`·`onprem`)과 내가 등록한 온프레미스 서버의 타깃. 서버 타깃은
    `connectionStatus` 가 `CONNECTED` 일 때만 배포할 수 있다.
    """
    targets = await service.search_targets(user.id)
    return ApiResponse(data=[TargetResponse.from_model(t) for t in targets])
