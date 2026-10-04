from typing import Annotated

from fastapi import APIRouter, Query, status

from app.dependencies import ConsoleServiceDep, CurrentUserDep
from app.schemas.console import (
    ConsoleAvailabilityResponse,
    ConsoleGatewayResponse,
    ConsoleSessionResponse,
    CreateConsoleSessionRequest,
)
from app.schemas.response import ApiResponse, error_responses

router = APIRouter(prefix="/api/v1/services", tags=["console"])
TargetQuery = Annotated[int, Query(alias="targetId", gt=0)]


@router.get(
    "/{service_id}/console",
    response_model=ApiResponse[ConsoleAvailabilityResponse],
    response_model_exclude_none=True,
    summary="서비스 콘솔을 열 수 있는지 조회",
    description=(
        "실행 중인 레플리카의 셸을 열 수 있는지 알려 준다. 열 수 없으면 `available=false` 와 "
        "`reason` 을 돌려주며(200), ticket 을 만들지 않는다. 콘솔 화면의 빈 상태 판단에 쓴다."
    ),
    responses=error_responses(401, 404, 422),
)
async def get_console_availability(
    service_id: int,
    target_id: TargetQuery,
    user: CurrentUserDep,
    service: ConsoleServiceDep,
) -> ApiResponse[ConsoleAvailabilityResponse]:
    availability = await service.get_availability(user.id, service_id, target_id)
    return ApiResponse(
        data=ConsoleAvailabilityResponse(
            available=availability.is_available, reason=availability.reason
        )
    )


@router.post(
    "/{service_id}/console/sessions",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[ConsoleSessionResponse],
    response_model_exclude_none=True,
    summary="서비스 콘솔 연결 ticket 발급",
    description=(
        "Console Gateway 에 붙는 60초짜리 ticket 을 발급한다. Pod 목록 조회용과 연결용으로 "
        "매번 새로 받는다(연결에는 한 번만 쓸 수 있다). 프로젝트 소유자만, 서비스에 연결된 "
        "타깃만 쓸 수 있다. Control API 는 클러스터에 접근하지 않는다."
    ),
    responses=error_responses(401, 404, 409, 422, 503),
)
async def create_console_session(
    service_id: int,
    body: CreateConsoleSessionRequest,
    user: CurrentUserDep,
    service: ConsoleServiceDep,
) -> ApiResponse[ConsoleSessionResponse]:
    issued = await service.create_session(user.id, service_id, body.target_id)
    return ApiResponse(
        data=ConsoleSessionResponse(
            session_id=issued.session_id,
            token=issued.token,
            expires_at=issued.expires_at,
            gateway=ConsoleGatewayResponse(
                http_url=issued.gateway.http_url, ws_url=issued.gateway.ws_url
            ),
        )
    )
