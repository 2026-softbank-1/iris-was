from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Header, Query, status

from app.dependencies import (
    CurrentUserDep,
    DeploymentHistoryServiceDep,
    DeploymentLogServiceDep,
    ManualDeploymentServiceDep,
    SessionDep,
)
from app.schemas.deployment import (
    CreateDeploymentRequest,
    DeploymentDetailResponse,
    DeploymentResponse,
)
from app.schemas.deployment_log import (
    BuildLogEntryResponse,
    BuildLogsResponse,
    DeploymentLogsResponse,
    NetworkLogEntryResponse,
    NetworkLogsResponse,
)
from app.schemas.observability import LogEntryResponse
from app.schemas.response import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    ApiResponse,
    Page,
    error_responses,
)

router = APIRouter(prefix="/api/v1", tags=["deployments"])

TargetQuery = Annotated[
    int | None,
    Query(
        alias="targetId",
        gt=0,
        description="없으면 이 배포가 반영된 첫 타깃. 배포에 없는 타깃이면 422.",
    ),
]
StartQuery = Annotated[
    datetime | None,
    Query(description="타임존이 있는 ISO 8601. 없으면 배포 기간 시작(최대 7일 전)."),
]
EndQuery = Annotated[
    datetime | None,
    Query(description="타임존이 있는 ISO 8601. 없으면 교체된 시각, 교체되지 않았으면 지금."),
]
LimitQuery = Annotated[
    int, Query(ge=1, le=1000, description="최근 limit 개를 시간순으로 돌려준다.")
]


@router.post(
    "/services/{service_id}/deployments",
    response_model=ApiResponse[DeploymentResponse],
    response_model_exclude_none=True,
    status_code=status.HTTP_201_CREATED,
    summary="배포 요청 생성 (수동·CLI 업로드·재배포·롤백·재시작·삭제)",
    responses=error_responses(401, 404, 409, 422, 502, 503),
)
async def create_deployment_request(
    service_id: int,
    body: CreateDeploymentRequest,
    user: CurrentUserDep,
    service: ManualDeploymentServiceDep,
    idempotency_key: Annotated[
        str | None,
        Header(
            max_length=64,
            description="같은 값으로 다시 보내면 새로 만들지 않고 처음 만든 배포 요청을 돌려준다.",
        ),
    ] = None,
) -> ApiResponse[DeploymentResponse]:
    request = await service.create_deployment_request(
        user.id,
        service_id,
        trigger_type=body.trigger_type,
        source_sha=body.source_sha,
        source_deployment_request_id=body.source_deployment_id,
        upload_id=body.upload_id,
        idempotency_key=idempotency_key,
        skip_variable_validation=body.skip_variable_validation,
    )
    return ApiResponse(data=DeploymentResponse.from_model(request))


@router.get(
    "/services/{service_id}/deployments",
    response_model=ApiResponse[Page[DeploymentResponse]],
    response_model_exclude_none=True,
    summary="배포 요청 목록 (최신순)",
    responses=error_responses(401, 404, 422),
)
async def search_deployment_requests(
    service_id: int,
    user: CurrentUserDep,
    service: DeploymentHistoryServiceDep,
    page: Annotated[int, Query(ge=0, description="0부터 시작")] = 0,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> ApiResponse[Page[DeploymentResponse]]:
    result = await service.search_deployment_requests(user.id, service_id, page, size)
    return ApiResponse(
        data=Page(
            items=[DeploymentResponse.from_model(r) for r in result.items],
            total=result.total,
            page=page,
            size=size,
        )
    )


@router.get(
    "/services/{service_id}/deployments/{deployment_id}",
    response_model=ApiResponse[DeploymentDetailResponse],
    response_model_exclude_none=True,
    summary="배포 요청 상세 (상태 이력·단계별 소요 시간)",
    responses=error_responses(401, 404, 422),
)
async def get_deployment_request(
    service_id: int,
    deployment_id: int,
    user: CurrentUserDep,
    service: DeploymentHistoryServiceDep,
) -> ApiResponse[DeploymentDetailResponse]:
    detail = await service.get_deployment_request(user.id, service_id, deployment_id)
    return ApiResponse(data=DeploymentDetailResponse.from_detail(detail))


@router.get(
    "/services/{service_id}/deployments/{deployment_id}/build-logs",
    response_model=ApiResponse[BuildLogsResponse],
    response_model_exclude_none=True,
    summary="배포 빌드 로그 조회 (CodeBuild)",
    description=(
        "처음부터 limit 개를 시간순으로 돌려주고 nextCursor 를 준다. 그 값을 cursor 로 다시 "
        "호출해 이어 읽는다. 진행 중인 빌드는 isComplete 가 true 가 될 때까지 폴링한다. "
        "롤백·재시작은 원본 배포의 빌드 로그를 돌려주며 loggedDeploymentId 가 그 배포다. "
        "빌드가 아직 로그를 만들지 않았거나 빌드가 없으면 entries 가 비어 있다. "
        "CloudWatch 를 읽도록 설정되지 않은 환경에서는 실패한 빌드의 저장된 끝부분만 "
        "돌려주고(isPartial), 그것도 없으면 503 이다."
    ),
    responses=error_responses(401, 404, 422, 502, 503),
)
async def search_build_logs(
    service_id: int,
    deployment_id: int,
    user: CurrentUserDep,
    service: DeploymentLogServiceDep,
    session: SessionDep,
    cursor: Annotated[
        str | None,
        Query(max_length=1024, description="이전 응답의 nextCursor. 없으면 처음부터 읽는다."),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=1000, description="한 번에 읽을 로그 수")] = 500,
) -> ApiResponse[BuildLogsResponse]:
    scope = await service.get_build_log_scope(user.id, service_id, deployment_id)
    await session.close()
    page = await service.search_build_logs(scope, cursor, limit)
    return ApiResponse(
        data=BuildLogsResponse(
            entries=[BuildLogEntryResponse.from_line(line) for line in page.entries],
            next_cursor=page.next_cursor,
            build_status=scope.build_status,
            is_complete=page.is_complete,
            is_partial=page.is_partial,
            logged_deployment_id=scope.logged_deployment_id,
        )
    )


@router.get(
    "/services/{service_id}/deployments/{deployment_id}/deploy-logs",
    response_model=ApiResponse[DeploymentLogsResponse],
    response_model_exclude_none=True,
    summary="배포 런타임 로그 조회 (이 배포의 release 로 거름)",
    description=(
        "서비스 앱 컨테이너 로그 중 이 배포의 release(iris_release_id)가 붙은 것만 돌려준다. "
        "release 가 없는 배포(빌드 실패 등)는 entries 가 비어 있다. "
        "stdout/stderr 구분은 수집 라벨이 없어 제공하지 않는다."
    ),
    responses=error_responses(401, 404, 422, 502, 503),
)
async def search_deploy_logs(
    service_id: int,
    deployment_id: int,
    user: CurrentUserDep,
    service: DeploymentLogServiceDep,
    session: SessionDep,
    target_id: TargetQuery = None,
    start: StartQuery = None,
    end: EndQuery = None,
    limit: LimitQuery = 200,
    search: Annotated[
        str, Query(max_length=500, description="대소문자를 구분하는 부분 문자열")
    ] = "",
) -> ApiResponse[DeploymentLogsResponse]:
    scope = await service.get_deploy_log_scope(
        user.id, service_id, deployment_id, target_id, start, end
    )
    await session.close()
    entries = await service.search_deploy_logs(scope, limit, search)
    return ApiResponse(
        data=DeploymentLogsResponse(
            entries=[LogEntryResponse.from_entry(entry) for entry in entries],
            is_truncated=len(entries) >= limit,
            start=scope.start,
            end=scope.end,
        )
    )


@router.get(
    "/services/{service_id}/deployments/{deployment_id}/network-logs",
    response_model=ApiResponse[NetworkLogsResponse],
    response_model_exclude_none=True,
    summary="배포 네트워크 로그 조회 (ALB 접근 로그)",
    description=(
        "ALB 접근 로그 중 이 서비스가 처리한 요청을 돌려준다. ALB 로그에는 배포 구분이 없어 "
        "이 배포가 서비스한 구간(성공한 때부터 교체될 때까지)으로 나눈다. 성공하지 못한 배포는 "
        "entries 가 비어 있다. URL·메서드·IP 는 수집하지 않아 상태 코드·바이트·응답 시간만 있다. "
        "ALB 로그 수집에 몇 분 지연이 있고 수집기가 배포되기 전에는 비어 있다."
    ),
    responses=error_responses(401, 404, 422, 502, 503),
)
async def search_network_logs(
    service_id: int,
    deployment_id: int,
    user: CurrentUserDep,
    service: DeploymentLogServiceDep,
    session: SessionDep,
    target_id: TargetQuery = None,
    start: StartQuery = None,
    end: EndQuery = None,
    limit: LimitQuery = 200,
    status_class: Annotated[
        Literal["2xx", "3xx", "4xx", "5xx"] | None,
        Query(alias="statusClass", description="ALB 최종 응답 코드 범위로 거른다."),
    ] = None,
) -> ApiResponse[NetworkLogsResponse]:
    scope = await service.get_network_log_scope(
        user.id, service_id, deployment_id, target_id, start, end
    )
    await session.close()
    entries = await service.search_network_logs(scope, limit, status_class)
    return ApiResponse(
        data=NetworkLogsResponse(
            entries=[NetworkLogEntryResponse.from_entry(entry) for entry in entries],
            is_truncated=len(entries) >= limit,
            start=scope.start,
            end=scope.end,
        )
    )
