from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Query, Response, status

from app.dependencies import CurrentUserDep, DiagnosisServiceDep, DiagnosisServiceOpenerDep
from app.schemas.diagnosis import DiagnosisResponse
from app.schemas.response import ApiResponse, error_responses
from app.services.diagnosis_service import run_diagnosis_in_background

router = APIRouter(prefix="/api/v1/services/{service_id}/deployments", tags=["diagnosis"])


@router.post(
    "/{deployment_id}/diagnose",
    response_model=ApiResponse[DiagnosisResponse],
    response_model_exclude_none=True,
    summary="실패한 배포를 AI 로 진단 시작",
    description=(
        "실패한 배포의 런타임 로그(와 가능하면 소스)를 에러 진단 에이전트에 보내 원인과 해결책을 "
        "받아 저장한다. 모델을 부르는 데 최대 2분 남짓 걸려 **바로 `202` 와 `status=RUNNING` 으로 "
        "답하고**, 진단은 서버가 이어서 실행한다. 결과는 `GET .../diagnosis` 를 `status` 가 "
        "`SUCCEEDED`·`FAILED` 가 될 때까지 폴링해 받는다. "
        "성공한 진단이 이미 있으면 모델을 다시 부르지 않고 `200` 으로 그 결과를 돌려주며, "
        "refresh=true 로 다시 진단한다. 해결책은 제안일 뿐 서버가 실행하지 않고, 배포 요청의 "
        "상태도 바꾸지 않는다."
    ),
    responses={
        **error_responses(401, 404, 409, 422, 503),
        status.HTTP_202_ACCEPTED: {
            "model": ApiResponse[DiagnosisResponse],
            "description": "진단을 시작했다(status=RUNNING). GET 으로 결과를 폴링한다",
        },
    },
)
async def diagnose_deployment_request(
    service_id: int,
    deployment_id: int,
    user: CurrentUserDep,
    service: DiagnosisServiceDep,
    open_service: DiagnosisServiceOpenerDep,
    background_tasks: BackgroundTasks,
    response: Response,
    refresh: Annotated[
        bool, Query(description="true 면 성공한 진단이 있어도 새로 진단한다(모델 비용이 든다).")
    ] = False,
) -> ApiResponse[DiagnosisResponse]:
    started = await service.start_diagnosis(user.id, service_id, deployment_id, refresh=refresh)
    if started.is_started:
        response.status_code = status.HTTP_202_ACCEPTED
        background_tasks.add_task(
            run_diagnosis_in_background,
            open_service,
            user.id,
            service_id,
            deployment_id,
            started.diagnosis.id,
        )
    return ApiResponse(data=DiagnosisResponse.from_model(started.diagnosis))


@router.get(
    "/{deployment_id}/diagnosis",
    response_model=ApiResponse[DiagnosisResponse],
    response_model_exclude_none=True,
    summary="배포의 가장 최근 AI 진단 조회",
    description="진단 상태(RUNNING·SUCCEEDED·FAILED)와 결과를 돌려준다. 진단 시작 뒤 폴링에 쓴다.",
    responses=error_responses(401, 404, 422),
)
async def get_deployment_diagnosis(
    service_id: int,
    deployment_id: int,
    user: CurrentUserDep,
    service: DiagnosisServiceDep,
) -> ApiResponse[DiagnosisResponse]:
    diagnosis = await service.get_diagnosis(user.id, service_id, deployment_id)
    return ApiResponse(data=DiagnosisResponse.from_model(diagnosis))
