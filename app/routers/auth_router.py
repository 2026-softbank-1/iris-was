from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import RedirectResponse

from app.core.cookies import OAUTH_NONCE_COOKIE, OAUTH_NONCE_MAX_AGE_SECONDS, set_http_only_cookie
from app.core.html_pages import cli_login_cancelled_page, cli_login_done_page
from app.dependencies import AuthServiceDep, SettingsDep
from app.schemas.response import error_responses
from app.services.auth_service import CliApprovalResult

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.get(
    "/github",
    response_model=None,
    status_code=status.HTTP_302_FOUND,
    summary="GitHub 로그인 시작",
    responses=error_responses(503),
)
async def start_github_login(service: AuthServiceDep, settings: SettingsDep) -> RedirectResponse:
    login = service.start_login()
    response = RedirectResponse(login.authorization_url, status_code=status.HTTP_302_FOUND)
    set_http_only_cookie(
        response, settings, OAUTH_NONCE_COOKIE, login.nonce, OAUTH_NONCE_MAX_AGE_SECONDS
    )
    return response


@router.get(
    "/github/callback",
    response_model=None,
    status_code=status.HTTP_302_FOUND,
    summary="GitHub 로그인 완료 (콜백)",
    responses={
        **error_responses(401, 404, 422, 502, 503),
        status.HTTP_200_OK: {
            "description": "CLI 로그인 승인(또는 취소) 안내 화면(HTML). 웹 로그인은 302 다.",
            "content": {"text/html": {}},
        },
    },
)
async def complete_github_login(
    request: Request,
    service: AuthServiceDep,
    settings: SettingsDep,
    code: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
) -> Response:
    """GitHub 가 로그인(또는 App 설치 직후 사용자 인증) 뒤 돌려보내는 주소.

    이 주소를 GitHub App 의 Callback URL 로 등록하고 "Request user authorization (OAuth)
    during installation" 을 켜 두면, 설치를 마친 사용자도 같은 흐름으로 설치 목록이 맞춰진다.

    state 가 CLI 로그인 승인용이면 웹 세션 쿠키를 만들지 않고 해당 CLI 로그인 세션을 승인(취소면
    거절)한 뒤 안내 화면을 보여 준다.
    """
    web_base_url = settings.web_base_url.rstrip("/")
    nonce = request.cookies.get(OAUTH_NONCE_COOKIE)
    if error is not None or code is None or state is None:
        if state is not None and await service.cancel_cli_login(state, nonce):
            response: Response = cli_login_cancelled_page()
            response.delete_cookie(OAUTH_NONCE_COOKIE, path="/")
            return response
        reason = urlencode({"error": error or "missing_code"})
        return RedirectResponse(f"{web_base_url}/login?{reason}", status_code=status.HTTP_302_FOUND)

    result = await service.complete_login(code, state, nonce)
    if isinstance(result, CliApprovalResult):
        response = cli_login_done_page()
    else:
        response = RedirectResponse(web_base_url, status_code=status.HTTP_302_FOUND)
        set_http_only_cookie(
            response,
            settings,
            settings.session_cookie_name,
            result.session_token,
            settings.session_ttl_minutes * 60,
        )
    response.delete_cookie(OAUTH_NONCE_COOKIE, path="/")
    return response


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="로그아웃",
)
async def logout(settings: SettingsDep) -> Response:
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(settings.session_cookie_name, path="/")
    return response
