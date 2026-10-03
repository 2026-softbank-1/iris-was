from typing import Annotated

from fastapi import APIRouter, Query

from app.dependencies import CurrentUserDep, RepairContextServiceDep
from app.schemas.repair import RepairContextResponse
from app.schemas.response import ApiResponse, error_responses

router = APIRouter(prefix="/api/v1/services/{service_id}/deployments", tags=["repair"])


@router.get(
    "/{deployment_id}/repair-context",
    response_model=ApiResponse[RepairContextResponse],
    # 진단 원문의 null 값까지 그대로 전달해야 해서 None 을 지우지 않는다.
    response_model_exclude_none=False,
    summary="코드 수정에 쓸 진단 원문과 고정된 소스 입력 조회",
    description=(
        "성공한 AI 진단 1건의 원본 `diagnosis-result.v3` 와, 그 진단이 본 시점의 소스 스냅샷"
        "(단기 다운로드 URL, 올릴 때 고정한 `archiveSha256`·`manifestSha256`), 저장소·브랜치를 "
        "돌려준다. 수정 에이전트가 이 값으로 소스를 검증하고 수정 전용 브랜치와 PR 을 만든다. "
        "읽기 전용이며 서버는 소스를 수정하지 않는다. 스냅샷은 하루 뒤 지워져 그 뒤에는 `409` 다."
    ),
    responses=error_responses(401, 404, 409, 422),
)
async def get_repair_context(
    service_id: int,
    deployment_id: int,
    user: CurrentUserDep,
    service: RepairContextServiceDep,
    diagnosis_id: Annotated[
        int, Query(alias="diagnosisId", gt=0, description="수정 근거로 쓸 성공한 진단의 ID")
    ],
) -> ApiResponse[RepairContextResponse]:
    context = await service.get_repair_context(user.id, service_id, deployment_id, diagnosis_id)
    return ApiResponse(data=context)
