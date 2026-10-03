from typing import Annotated

from fastapi import APIRouter, Header, status

from app.dependencies import CurrentUserDep, ServiceScalingServiceDep
from app.schemas.response import ApiResponse, error_responses
from app.schemas.scaling import ServiceScalingResponse, UpdateServiceScalingRequest

router = APIRouter(prefix="/api/v1/services", tags=["services"])


@router.get(
    "/{service_id}/scaling",
    response_model=ApiResponse[ServiceScalingResponse],
    response_model_exclude_none=True,
    summary="서비스의 원하는 Pod 수·CPU·메모리 사양 조회",
    responses=error_responses(401, 404, 422, 503),
)
async def get_scaling(
    service_id: int, user: CurrentUserDep, service: ServiceScalingServiceDep
) -> ApiResponse[ServiceScalingResponse]:
    detail = await service.get_scaling(user.id, service_id)
    return ApiResponse(data=ServiceScalingResponse.from_detail(detail))


@router.put(
    "/{service_id}/scaling",
    response_model=ApiResponse[ServiceScalingResponse],
    response_model_exclude_none=True,
    status_code=status.HTTP_202_ACCEPTED,
    summary="배포된 서비스의 Pod 수·CPU·메모리 사양 교체",
    description=(
        "replicas(0~10)와 resources.requests/limits 전체를 보낸다. "
        "성공한 현재 이미지를 재사용하는 RESTART 요청으로 빌드 없이 비동기 적용한다. "
        "202는 요청 접수이며 deploymentRequestId로 배포 상태를 조회한다. "
        "이후 배포에도 저장한 사양을 사용한다."
    ),
    responses=error_responses(401, 404, 409, 422, 503),
)
async def update_scaling(
    service_id: int,
    body: UpdateServiceScalingRequest,
    user: CurrentUserDep,
    service: ServiceScalingServiceDep,
    idempotency_key: Annotated[
        str | None,
        Header(
            min_length=1,
            max_length=64,
            description="같은 설정과 키로 재전송하면 같은 요청을 반환한다.",
        ),
    ] = None,
) -> ApiResponse[ServiceScalingResponse]:
    detail = await service.update_scaling(
        user.id, service_id, body.to_config(), idempotency_key=idempotency_key
    )
    return ApiResponse(data=ServiceScalingResponse.from_detail(detail))
