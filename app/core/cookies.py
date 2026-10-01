from fastapi import Response

from app.core.config import Settings

OAUTH_NONCE_COOKIE = "anydeploy_oauth_nonce"
OAUTH_NONCE_MAX_AGE_SECONDS = 600


def set_http_only_cookie(
    response: Response, settings: Settings, name: str, value: str, max_age: int
) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=settings.is_session_cookie_secure,
        path="/",
    )
