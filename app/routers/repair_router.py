from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Header, Query, Response

from app.dependencies import (
    AutomaticRepairServiceDep,
    CurrentUserDep,
    RepairGithubAuthServiceDep,
    RepairPublicationServiceDep,
    RepairServiceDep,
    RepairServiceOpenerDep,
)
from app.schemas.repair import (
    CreateAutomaticRepairRequest,
    CreateRepairRequest,
    RepairAccessResponse,
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
        "기존 WAS 세션 쿠키 또는 Bearer 토큰으로 인증하며 GitHub PAT를 입력하지 않는다. "
        "App과 설치에 Contents·Pull requests 읽기/쓰기 승인이 모두 필요하다. "
        "쓰기 권한이 없으면 403 FORBIDDEN이며, /api/v1/github/install로 설치/권한 승인을 진행한다. "
        "토큰은 코디네이터 메모리에서만 사용하고 만료 60초 전에 같은 API로 갱신한다. "
        "생성 API·로그·작업 기록에 저장하지 않는다. "
        "이 API는 브랜치 생성·PR 머지·배포를 실행하지 않는다."
    ),
    responses={
        **error_responses(401, 403, 404, 409, 422, 502, 503),
        200: {
            "description": "해당 저장소 한정 Contents·Pull requests write 단기 설치 토큰",
            "headers": {
                "Cache-Control": {"schema": {"type": "string", "const": "no-store"}},
                "Pragma": {"schema": {"type": "string", "const": "no-cache"}},
            },
        },
        403: {
            **error_responses(403)[403],
            "description": (
                "설치/저장소 접근 불가 또는 App·설치의 Contents/Pull requests 쓰기 권한 부족"
            ),
            "content": {
                "application/json": {
                    "examples": {
                        "missing_write_permissions": {
                            "summary": "App 쓰기 권한 미승인",
                            "value": {
                                "success": False,
                                "code": "FORBIDDEN",
                                "message": "github app requires Contents and Pull requests write",
                            },
                        },
                        "repository_not_accessible": {
                            "summary": "설치 또는 저장소 접근 권한 없음",
                            "value": {
                                "success": False,
                                "code": "REPOSITORY_NOT_ACCESSIBLE",
                                "message": (
                                    "repository is not accessible; "
                                    "install the github app and grant access"
                                ),
                            },
                        },
                    }
                }
            },
        },
        404: {
            **error_responses(404)[404],
            "description": "서비스가 없거나 현재 로그인 사용자의 서비스가 아님 (SERVICE_NOT_FOUND)",
        },
        409: {
            **error_responses(409)[409],
            "description": "요청 repository가 현재 서비스 소스 저장소와 다름 (CONFLICT)",
        },
    },
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


@router.get(
    "/repair-access",
    response_model=ApiResponse[RepairAccessResponse],
    summary="GitHub 코드수정 쓰기 권한 확인 (토큰 반환 없음)",
    responses=error_responses(401, 404, 422, 502, 503),
)
async def check_repair_access(
    service_id: int, user: CurrentUserDep, service: RepairGithubAuthServiceDep, response: Response
) -> ApiResponse[RepairAccessResponse]:
    response.headers["Cache-Control"] = "no-store"
    return ApiResponse(data=await service.check_access(user.id, service_id))


@router.get(
    "/deployments/{deployment_id}/repairs/latest",
    response_model=ApiResponse[RepairResponse],
    response_model_exclude_none=True,
    summary="선택한 진단의 최근 코드수정 작업 조회",
    responses=error_responses(401, 404, 422),
)
async def latest_repair(
    service_id: int,
    deployment_id: int,
    user: CurrentUserDep,
    service: RepairServiceDep,
    diagnosis_id: Annotated[int, Query(alias="diagnosisId", gt=0)],
) -> ApiResponse[RepairResponse]:
    return ApiResponse(
        data=RepairResponse.from_model(
            await service.latest_repair(user.id, service_id, deployment_id, diagnosis_id)
        )
    )


@router.post(
    "/repairs/{repair_id}/publish",
    response_model=ApiResponse[RepairResponse],
    response_model_exclude_none=True,
    summary="코드수정 후보로 핫픽스 브랜치와 PR 생성",
    responses=error_responses(401, 403, 404, 409, 422, 502, 503),
)
async def publish_repair(
    service_id: int, repair_id: int, user: CurrentUserDep, service: RepairPublicationServiceDep
) -> ApiResponse[RepairResponse]:
    return ApiResponse(
        data=RepairResponse.from_model(
            await service.execute(user.id, service_id, repair_id, "publish")
        )
    )


@router.post(
    "/repairs/{repair_id}/merge",
    response_model=ApiResponse[RepairResponse],
    response_model_exclude_none=True,
    summary="검토한 핫픽스 PR을 main에 머지 (GitHub 보호 규칙 적용)",
    responses=error_responses(401, 403, 404, 409, 422, 502, 503),
)
async def merge_repair(
    service_id: int, repair_id: int, user: CurrentUserDep, service: RepairPublicationServiceDep
) -> ApiResponse[RepairResponse]:
    return ApiResponse(
        data=RepairResponse.from_model(
            await service.execute(user.id, service_id, repair_id, "merge")
        )
    )


@router.post(
    "/deployments/{deployment_id}/auto-repair",
    response_model=ApiResponse[RepairResponse],
    status_code=202,
    response_model_exclude_none=True,
    summary="AI 수정 한 번으로 후보 생성·핫픽스 PR·main 자동 머지",
    responses=error_responses(401, 403, 404, 409, 422, 502, 503),
)
async def start_automatic_repair(
    service_id: int,
    deployment_id: int,
    body: CreateAutomaticRepairRequest,
    user: CurrentUserDep,
    service: AutomaticRepairServiceDep,
    idempotency_key: Annotated[
        str, Header(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    ],
) -> ApiResponse[RepairResponse]:
    started = await service.start(
        user.id, service_id, deployment_id, body.diagnosis_id, idempotency_key
    )
    return ApiResponse(data=RepairResponse.from_model(started.repair))


@router.post(
    "/repairs/{repair_id}/auto",
    response_model=ApiResponse[RepairResponse],
    status_code=202,
    response_model_exclude_none=True,
    summary="기존 후보로 자동 핫픽스 게시·머지 시작 또는 재개",
    responses=error_responses(401, 403, 404, 409, 422, 502, 503),
)
async def resume_automatic_repair(
    service_id: int, repair_id: int, user: CurrentUserDep, service: AutomaticRepairServiceDep
) -> ApiResponse[RepairResponse]:
    return ApiResponse(
        data=RepairResponse.from_model(await service.resume(user.id, service_id, repair_id))
    )
