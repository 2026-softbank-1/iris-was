from typing import Annotated

from fastapi import APIRouter, Query, status
from fastapi.responses import RedirectResponse

from app.core.cookies import OAUTH_NONCE_COOKIE, OAUTH_NONCE_MAX_AGE_SECONDS, set_http_only_cookie
from app.core.exceptions import NotConfiguredError
from app.dependencies import (
    AuthServiceDep,
    CurrentUserDep,
    SettingsDep,
    SourceRepositoryServiceDep,
)
from app.schemas.github import BranchResponse, InstallationResponse, RepositoryResponse
from app.schemas.response import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    ApiResponse,
    Page,
    error_responses,
)

router = APIRouter(prefix="/api/v1/github", tags=["github"])


@router.get(
    "/install",
    response_model=None,
    status_code=status.HTTP_302_FOUND,
    summary="GitHub App 설치 시작",
    responses=error_responses(503),
)
async def start_github_app_installation(
    auth_service: AuthServiceDep, settings: SettingsDep
) -> RedirectResponse:
    """GitHub App 설치 페이지로 보낸다. 설치를 마치면 로그인 콜백으로 돌아온다."""
    if settings.github_app_slug is None:
        raise NotConfiguredError("github app is not configured", setting="GITHUB_APP_SLUG")
    installation = auth_service.start_installation(settings.github_app_slug)
    response = RedirectResponse(installation.authorization_url, status_code=status.HTTP_302_FOUND)
    set_http_only_cookie(
        response, settings, OAUTH_NONCE_COOKIE, installation.nonce, OAUTH_NONCE_MAX_AGE_SECONDS
    )
    return response


@router.get(
    "/installations",
    response_model=ApiResponse[list[InstallationResponse]],
    response_model_exclude_none=True,
    summary="내 GitHub App 설치 목록",
    responses=error_responses(401, 503),
)
async def search_installations(
    user: CurrentUserDep, service: SourceRepositoryServiceDep
) -> ApiResponse[list[InstallationResponse]]:
    installations = await service.search_installations(user.id)
    return ApiResponse(data=[InstallationResponse.from_model(i) for i in installations])


@router.get(
    "/repos",
    response_model=ApiResponse[Page[RepositoryResponse]],
    response_model_exclude_none=True,
    summary="접근 가능한 저장소 검색",
    responses=error_responses(401, 422, 502, 503),
)
async def search_repositories(
    user: CurrentUserDep,
    service: SourceRepositoryServiceDep,
    q: Annotated[str | None, Query(max_length=200)] = None,
    installation_id: Annotated[int | None, Query(alias="installationId")] = None,
    page: Annotated[int, Query(ge=0)] = 0,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> ApiResponse[Page[RepositoryResponse]]:
    candidates = await service.search_repositories(user.id, q, installation_id)
    repositories = [RepositoryResponse.from_candidate(c) for c in candidates]
    return ApiResponse(data=Page.from_items(repositories, page, size))


@router.get(
    "/repos/resolve",
    response_model=ApiResponse[RepositoryResponse],
    response_model_exclude_none=True,
    summary="GitHub 주소로 저장소 확인",
    responses=error_responses(401, 403, 422, 502, 503),
)
async def resolve_repository(
    user: CurrentUserDep,
    service: SourceRepositoryServiceDep,
    url: Annotated[str, Query(min_length=1, max_length=500)],
) -> ApiResponse[RepositoryResponse]:
    """붙여넣은 GitHub 주소를 저장소로 해석하고 접근 권한을 확인한다."""
    candidate = await service.resolve_repository(user.id, url)
    return ApiResponse(data=RepositoryResponse.from_candidate(candidate))


@router.get(
    "/repos/{owner}/{repo}/branches",
    response_model=ApiResponse[list[BranchResponse]],
    response_model_exclude_none=True,
    summary="저장소 브랜치 목록",
    responses=error_responses(401, 403, 422, 502, 503),
)
async def search_branches(
    owner: str, repo: str, user: CurrentUserDep, service: SourceRepositoryServiceDep
) -> ApiResponse[list[BranchResponse]]:
    branches = await service.search_branches(user.id, f"{owner}/{repo}")
    return ApiResponse(data=[BranchResponse.from_info(b) for b in branches])
