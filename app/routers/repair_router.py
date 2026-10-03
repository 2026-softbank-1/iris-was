from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Header, Response

from app.dependencies import (
    CurrentUserDep,
    RepairGithubAuthServiceDep,
    RepairServiceDep,
    RepairServiceOpenerDep,
)
from app.schemas.repair import (
    CreateRepairRequest,
    RepairGithubTokenRequest,
    RepairGithubTokenResponse,
    RepairResponse,
)
from app.schemas.response import ApiResponse, error_responses
from app.services.repair_service import run_repair_in_background

router = APIRouter(prefix="/api/v1/services/{service_id}", tags=["repair"])


@router.post(
    "/deployments/{deployment_id}/repairs",
    response_model=ApiResponse[RepairResponse],
    response_model_exclude_none=True,
    summary="AI 코드 수정 후보 생성 시작",
    responses={
        **error_responses(401, 404, 409, 422, 503),
        202: {
            "model": ApiResponse[RepairResponse],
            "description": "Candidate attempt accepted; poll the repair ID",
        },
    },
)
async def create_repair(
    service_id: int,
    deployment_id: int,
    body: CreateRepairRequest,
    user: CurrentUserDep,
    service: RepairServiceDep,
    open_service: RepairServiceOpenerDep,
    background_tasks: BackgroundTasks,
    response: Response,
    idempotency_key: Annotated[
        str, Header(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    ],
) -> ApiResponse[RepairResponse]:
    started = await service.start_repair(
        user.id, service_id, deployment_id, body.diagnosis_id, body.plan_ids, idempotency_key
    )
    if started.is_started:
        response.status_code = 202
        background_tasks.add_task(
            run_repair_in_background, open_service, user.id, service_id, started.repair.id
        )
    return ApiResponse(data=RepairResponse.from_model(started.repair))


@router.get(
    "/repairs/{repair_id}",
    response_model=ApiResponse[RepairResponse],
    response_model_exclude_none=True,
    summary="AI 코드 수정 후보와 상태 조회",
    responses=error_responses(401, 404, 422),
)
async def get_repair(
    service_id: int, repair_id: int, user: CurrentUserDep, service: RepairServiceDep
) -> ApiResponse[RepairResponse]:
    repair = await service.get_repair(user.id, service_id, repair_id)
    return ApiResponse(data=RepairResponse.from_model(repair))


@router.get(
    "/repairs/{repair_id}/artifacts/{name}",
    response_class=Response,
    summary="무결성 검증한 코드 수정 artifact 다운로드",
    responses=error_responses(401, 404, 422, 502, 503),
)
async def get_repair_artifact(
    service_id: int, repair_id: int, name: str, user: CurrentUserDep, service: RepairServiceDep
) -> Response:
    content = await service.get_artifact(user.id, service_id, repair_id, name)
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.post(
    "/repair-github-token",
    response_model=ApiResponse[RepairGithubTokenResponse],
    response_model_exclude_none=True,
    summary="WAS 로그인 사용자의 서비스용 GitHub 단기 쓰기 토큰 발급",
    description=(
        "서비스 소유권, 로그인 시 연결된 GitHub App 설치와 현재 저장소 접근권한을 확인한다. "
        "해당 소스 저장소 하나에 Contents·Pull requests write 토큰을 발급한다. "
        "수정 코디네이터만 사용하며 생성 API·로그·작업 기록에 저장하지 않는다."
    ),
    responses=error_responses(401, 403, 404, 409, 422, 502, 503),
)
async def issue_repair_github_token(
    service_id: int,
    body: RepairGithubTokenRequest,
    user: CurrentUserDep,
    service: RepairGithubAuthServiceDep,
    response: Response,
) -> ApiResponse[RepairGithubTokenResponse]:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    token = await service.issue_token(user.id, service_id, body.repository)
    return ApiResponse(data=token)
