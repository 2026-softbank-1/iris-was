from fastapi import APIRouter, Request, status
from fastapi.responses import RedirectResponse

from app.core.config import Settings
from app.core.cookies import OAUTH_NONCE_COOKIE, OAUTH_NONCE_MAX_AGE_SECONDS, set_http_only_cookie
from app.dependencies import AuthServiceDep, CliLoginServiceDep, SettingsDep
from app.schemas.cli_login import (
    CliLoginSessionResponse,
    CliLoginTokenResponse,
    PollCliLoginTokenRequest,
)
from app.schemas.response import ApiResponse, error_responses

router = APIRouter(prefix="/api/v1/auth/cli/sessions", tags=["auth"])


def _build_verification_url(request: Request, settings: Settings, session_id: str) -> str:
    path = request.app.url_path_for("authorize_cli_login", session_id=session_id)
    base_url = settings.api_base_url or str(request.base_url)
    return f"{base_url.rstrip('/')}{path}"


@router.post(
    "",
    response_model=ApiResponse[CliLoginSessionResponse],
    response_model_exclude_none=True,
    status_code=status.HTTP_201_CREATED,
    summary="CLI 로그인 세션 생성",
    responses=error_responses(503),
)
async def create_cli_login_session(
    request: Request, service: CliLoginServiceDep, settings: SettingsDep
) -> ApiResponse[CliLoginSessionResponse]:
    """인증 없이 호출한다. 응답의 `verificationUrl` 을 브라우저로 열어 GitHub 로그인을 승인하면,
    CLI 는 `sessionId`·`pollSecret` 으로 `/token` 을 폴링해 세션 토큰을 받는다.
    """
    start = await service.create_session()
    verification_url = _build_verification_url(request, settings, start.session_id)
    return ApiResponse(data=CliLoginSessionResponse.from_start(start, verification_url))


@router.get(
    "/{session_id}/authorize",
    response_model=None,
    status_code=status.HTTP_302_FOUND,
    summary="CLI 로그인 승인 (GitHub 로그인으로 이동)",
    responses=error_responses(404, 422, 503),
)
async def authorize_cli_login(
    session_id: str, service: AuthServiceDep, settings: SettingsDep
) -> RedirectResponse:
    """CLI 가 알려 준 주소를 브라우저로 여는 곳. GitHub 로그인이 끝나면 `/auth/github/callback` 이
    이 세션을 승인으로 바꾸고 "터미널로 돌아가세요" 화면을 보여 준다.
    만료됐거나 모르는 세션이면 404 다.
    """
    login = await service.start_cli_login(session_id)
    response = RedirectResponse(login.authorization_url, status_code=status.HTTP_302_FOUND)
    set_http_only_cookie(
        response, settings, OAUTH_NONCE_COOKIE, login.nonce, OAUTH_NONCE_MAX_AGE_SECONDS
    )
    return response


@router.post(
    "/{session_id}/token",
    response_model=ApiResponse[CliLoginTokenResponse],
    response_model_exclude_none=True,
    summary="CLI 로그인 폴링 (승인되면 토큰 발급)",
    responses=error_responses(401, 404, 422, 429, 503),
)
async def poll_cli_login_token(
    session_id: str, body: PollCliLoginTokenRequest, service: CliLoginServiceDep
) -> ApiResponse[CliLoginTokenResponse]:
    """`interval` 초마다 호출한다. `APPROVED` 일 때 `accessToken` 은 처음 한 번만 내려가고,
    그 뒤에는 `EXPIRED` 다. `pollSecret` 이 틀리면 401 이다(이 API 의 401 은 로그인 필요가 아니다).
    """
    poll = await service.poll_token(session_id, body.poll_secret)
    return ApiResponse(data=CliLoginTokenResponse.from_poll(poll))
