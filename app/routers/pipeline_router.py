from typing import Annotated

from fastapi import APIRouter, Header, status

from app.dependencies import CurrentUserDep, PipelineServiceDep
from app.schemas.pipeline import PipelineAnswers, PipelineResponse, StartPipelineRequest
from app.schemas.response import ApiResponse, error_responses

router = APIRouter(prefix="/api/v1/services/{service_id}/pipelines", tags=["pipelines"])


@router.post(
    "",
    response_model=ApiResponse[PipelineResponse],
    response_model_exclude_none=True,
    status_code=status.HTTP_202_ACCEPTED,
    summary="분석·계획·빌드·배포 파이프라인 접수",
    responses=error_responses(401, 403, 404, 409, 422, 502, 503),
)
async def start_pipeline(
    service_id: int,
    body: StartPipelineRequest,
    user: CurrentUserDep,
    service: PipelineServiceDep,
    idempotency_key: Annotated[str | None, Header(max_length=64)] = None,
) -> ApiResponse[PipelineResponse]:
    run = await service.create_pipeline(user.id, service_id, body, idempotency_key)
    return ApiResponse(data=PipelineResponse.from_model(run))


@router.get(
    "",
    response_model=ApiResponse[PipelineResponse],
    response_model_exclude_none=True,
    summary="최근 원클릭 파이프라인 상태 조회",
    responses=error_responses(401, 404, 422),
)
async def get_pipeline(
    service_id: int, user: CurrentUserDep, service: PipelineServiceDep
) -> ApiResponse[PipelineResponse]:
    return ApiResponse(
        data=PipelineResponse.from_model(await service.get_latest(user.id, service_id))
    )


@router.post(
    "/{pipeline_id}/answers",
    response_model=ApiResponse[PipelineResponse],
    response_model_exclude_none=True,
    status_code=status.HTTP_202_ACCEPTED,
    summary="부족 정보 확인 후 계획·배포 재개",
    responses=error_responses(401, 403, 404, 409, 422, 502, 503),
)
async def answer_pipeline(
    service_id: int,
    pipeline_id: str,
    body: PipelineAnswers,
    user: CurrentUserDep,
    service: PipelineServiceDep,
) -> ApiResponse[PipelineResponse]:
    return ApiResponse(
        data=PipelineResponse.from_model(
            await service.answer(user.id, service_id, pipeline_id, body)
        )
    )


@router.post(
    "/{pipeline_id}/cancel",
    response_model=ApiResponse[PipelineResponse],
    response_model_exclude_none=True,
    summary="실행 전 파이프라인 취소",
    responses=error_responses(401, 404, 409, 422),
)
async def cancel_pipeline(
    service_id: int, pipeline_id: str, user: CurrentUserDep, service: PipelineServiceDep
) -> ApiResponse[PipelineResponse]:
    return ApiResponse(
        data=PipelineResponse.from_model(await service.cancel(user.id, service_id, pipeline_id))
    )
