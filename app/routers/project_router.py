from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.dependencies import CurrentUserDep, ProjectServiceDep
from app.schemas.project import ProjectCreateRequest, ProjectResponse, ProjectUpdateRequest
from app.schemas.response import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    ApiResponse,
    Page,
    error_responses,
)

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])


@router.post(
    "",
    response_model=ApiResponse[ProjectResponse],
    response_model_exclude_none=True,
    status_code=status.HTTP_201_CREATED,
    summary="프로젝트 생성",
    responses=error_responses(401, 409, 422),
)
async def create_project(
    body: ProjectCreateRequest, user: CurrentUserDep, service: ProjectServiceDep
) -> ApiResponse[ProjectResponse]:
    summary = await service.create_project(user.id, body.name, body.description)
    return ApiResponse(data=ProjectResponse.from_summary(summary))


@router.get(
    "",
    response_model=ApiResponse[Page[ProjectResponse]],
    response_model_exclude_none=True,
    summary="프로젝트 목록",
    responses=error_responses(401, 422),
)
async def search_projects(
    user: CurrentUserDep,
    service: ProjectServiceDep,
    page: Annotated[int, Query(ge=0)] = 0,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> ApiResponse[Page[ProjectResponse]]:
    summaries = await service.search_projects(user.id)
    projects = [ProjectResponse.from_summary(s) for s in summaries]
    return ApiResponse(data=Page.from_items(projects, page, size))


@router.get(
    "/{project_id}",
    response_model=ApiResponse[ProjectResponse],
    response_model_exclude_none=True,
    summary="프로젝트 조회",
    responses=error_responses(401, 404, 422),
)
async def get_project(
    project_id: int, user: CurrentUserDep, service: ProjectServiceDep
) -> ApiResponse[ProjectResponse]:
    summary = await service.get_project(user.id, project_id)
    return ApiResponse(data=ProjectResponse.from_summary(summary))


@router.patch(
    "/{project_id}",
    response_model=ApiResponse[ProjectResponse],
    response_model_exclude_none=True,
    summary="프로젝트 수정",
    responses=error_responses(401, 404, 409, 422),
)
async def update_project(
    project_id: int,
    body: ProjectUpdateRequest,
    user: CurrentUserDep,
    service: ProjectServiceDep,
) -> ApiResponse[ProjectResponse]:
    summary = await service.update_project(user.id, project_id, body.model_dump(exclude_unset=True))
    return ApiResponse(data=ProjectResponse.from_summary(summary))


@router.delete(
    "/{project_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="프로젝트 삭제 (떠 있는 앱도 함께 내린다)",
    responses=error_responses(401, 404, 409, 422),
)
async def delete_project(
    project_id: int, user: CurrentUserDep, service: ProjectServiceDep
) -> Response:
    await service.delete_project(user.id, project_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
