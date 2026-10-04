from fastapi import APIRouter, status

from app.dependencies import CurrentUserDep, RepositoryAnalysisServiceDep
from app.schemas.repository_analysis import (
    ApplyRepositoryAnalysisRequest,
    ApplyRepositoryAnalysisResponse,
    CreateRepositoryAnalysisRequest,
    RepositoryAnalysisResponse,
)
from app.schemas.response import ApiResponse, error_responses

router = APIRouter(prefix="/api/v1", tags=["repository-analyses"])


@router.post(
    "/projects/{project_id}/repository-analyses",
    response_model=ApiResponse[RepositoryAnalysisResponse],
    response_model_exclude_none=True,
    status_code=status.HTTP_202_ACCEPTED,
    summary="레포 구성 분석 접수 (Build Worker 가 실행, GET 으로 폴링)",
    responses=error_responses(401, 403, 404, 422, 502, 503),
)
async def create_repository_analysis(
    project_id: int,
    body: CreateRepositoryAnalysisRequest,
    user: CurrentUserDep,
    service: RepositoryAnalysisServiceDep,
) -> ApiResponse[RepositoryAnalysisResponse]:
    analysis = await service.create_analysis(
        user.id,
        project_id,
        body.source_repository_url,
        body.source_branch,
        body.root_directory,
        body.mode,
        body.github_installation_id,
    )
    return ApiResponse(data=RepositoryAnalysisResponse.from_model(analysis))


@router.get(
    "/projects/{project_id}/repository-analyses/{analysis_id}",
    response_model=ApiResponse[RepositoryAnalysisResponse],
    response_model_exclude_none=True,
    summary="레포 구성 분석 조회",
    responses=error_responses(401, 404, 422, 503),
)
async def get_repository_analysis(
    project_id: int,
    analysis_id: int,
    user: CurrentUserDep,
    service: RepositoryAnalysisServiceDep,
) -> ApiResponse[RepositoryAnalysisResponse]:
    analysis = await service.get_analysis(user.id, project_id, analysis_id)
    return ApiResponse(data=RepositoryAnalysisResponse.from_model(analysis))


@router.post(
    "/projects/{project_id}/repository-analyses/{analysis_id}/apply",
    response_model=ApiResponse[ApplyRepositoryAnalysisResponse],
    response_model_exclude_none=True,
    status_code=status.HTTP_201_CREATED,
    summary="레포 구성 분석의 배포 단위마다 서비스 생성 (멱등)",
    responses=error_responses(401, 403, 404, 409, 422, 502, 503),
)
async def apply_repository_analysis(
    project_id: int,
    analysis_id: int,
    body: ApplyRepositoryAnalysisRequest,
    user: CurrentUserDep,
    service: RepositoryAnalysisServiceDep,
) -> ApiResponse[ApplyRepositoryAnalysisResponse]:
    applied = await service.apply_analysis(
        user.id,
        project_id,
        analysis_id,
        [unit.to_selection() for unit in body.units],
        should_deploy=body.deploy,
        target_ids=body.target_ids,
        is_auto_deploy=body.is_auto_deploy,
        dependencies=(
            [d.to_selection() for d in body.dependencies] if body.dependencies is not None else None
        ),
        skip_variable_validation=body.skip_variable_validation,
    )
    return ApiResponse(data=ApplyRepositoryAnalysisResponse.from_applied(applied))
