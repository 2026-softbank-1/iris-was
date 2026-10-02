from fastapi import APIRouter

from app.dependencies import CurrentUserDep
from app.schemas.auth import UserResponse
from app.schemas.response import ApiResponse, error_responses

router = APIRouter(prefix="/api/v1/me", tags=["user"])


@router.get(
    "",
    response_model=ApiResponse[UserResponse],
    response_model_exclude_none=True,
    summary="현재 사용자",
    responses=error_responses(401),
)
async def get_me(user: CurrentUserDep) -> ApiResponse[UserResponse]:
    return ApiResponse(data=UserResponse.from_model(user))
