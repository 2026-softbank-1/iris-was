import time
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Header, Query
from fastapi.responses import StreamingResponse

from app.core.exceptions import InvalidInputError
from app.dependencies import CurrentUserDep, ObservabilityServiceDep, SessionDep
from app.schemas.observability import LogEntryResponse, LogsResponse, MetricSeriesResponse
from app.schemas.response import ApiResponse, error_responses

router = APIRouter(prefix="/api/v1/services", tags=["observability"])
TargetQuery = Annotated[int, Query(alias="targetId", gt=0)]
LimitQuery = Annotated[int, Query(ge=1, le=1000)]
SearchQuery = Annotated[str, Query(max_length=500)]


@router.get(
    "/{service_id}/logs",
    response_model=ApiResponse[LogsResponse],
    response_model_exclude_none=True,
    summary="서비스 런타임 로그 조회",
    responses=error_responses(401, 404, 422, 502, 503),
)
async def search_logs(
    service_id: int,
    target_id: TargetQuery,
    start: datetime,
    end: datetime,
    user: CurrentUserDep,
    service: ObservabilityServiceDep,
    session: SessionDep,
    limit: LimitQuery = 200,
    search: SearchQuery = "",
) -> ApiResponse[LogsResponse]:
    namespace = await service.get_scope(user.id, service_id, target_id)
    service.validate_range(start, end)
    await session.close()
    entries = await service.search_logs(
        target_id,
        namespace,
        int(start.timestamp() * 1e9),
        int(end.timestamp() * 1e9),
        limit,
        search,
    )
    return ApiResponse(
        data=LogsResponse(
            entries=[LogEntryResponse.from_entry(entry) for entry in entries],
            is_truncated=len(entries) >= limit,
        )
    )


@router.get(
    "/{service_id}/metrics",
    response_model=ApiResponse[list[MetricSeriesResponse]],
    response_model_exclude_none=True,
    summary="서비스 CPU·메모리·네트워크 시계열 조회",
    responses=error_responses(401, 404, 422, 502, 503),
)
async def search_metrics(
    service_id: int,
    target_id: TargetQuery,
    start: datetime,
    end: datetime,
    user: CurrentUserDep,
    service: ObservabilityServiceDep,
    session: SessionDep,
    step: Annotated[int, Query(ge=15, le=86400)] = 60,
) -> ApiResponse[list[MetricSeriesResponse]]:
    namespace = await service.get_scope(user.id, service_id, target_id)
    service.validate_range(start, end)
    await session.close()
    series = await service.search_metrics(target_id, namespace, start, end, step)
    return ApiResponse(data=[MetricSeriesResponse.from_series(item) for item in series])


@router.get(
    "/{service_id}/logs/stream",
    response_model=None,
    response_class=StreamingResponse,
    summary="서비스 런타임 로그 SSE 구독",
    description=(
        "logs 이벤트의 data는 로그 배열, id는 다음 조회 시작 시각(ns)이다. "
        "Last-Event-ID 또는 cursor로 재연결한다. 5분마다 재연결하여 권한을 재확인한다. "
        "overflow/error 이벤트를 받으면 연결을 닫고 과거 로그 API로 조회한다."
    ),
    responses={
        **error_responses(401, 404, 422, 502, 503),
        200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}},
    },
)
async def stream_logs(
    service_id: int,
    target_id: TargetQuery,
    user: CurrentUserDep,
    service: ObservabilityServiceDep,
    session: SessionDep,
    search: SearchQuery = "",
    cursor: Annotated[str | None, Query(max_length=20)] = None,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID", max_length=20)] = None,
) -> StreamingResponse:
    namespace = await service.get_scope(user.id, service_id, target_id)
    now_ns = time.time_ns()
    resume_cursor = last_event_id or cursor
    try:
        start_ns = int(resume_cursor) if resume_cursor is not None else now_ns - 10 * 10**9
    except ValueError as exc:
        raise InvalidInputError("cursor must be a Unix timestamp in nanoseconds") from exc
    if not now_ns - 7 * 86400 * 10**9 <= start_ns <= now_ns:
        raise InvalidInputError("cursor must be within the last 7 days")
    await session.close()
    # 초기 조회 실패는 스트림 헤더를 보내기 전에 JSON 에러 응답으로 돌려준다.
    initial = await service.prepare_stream(target_id, namespace, start_ns, now_ns, search)
    return StreamingResponse(
        service.stream_logs(target_id, namespace, start_ns, search, initial),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
