"""세션 토큰과 OAuth state 의 서명·검증. 모두 HS256 JWT 로 만든다."""

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt

from app.core.exceptions import UnauthorizedError

TokenPurpose = Literal["session", "oauth_state"]

_ALGORITHM = "HS256"
OAUTH_STATE_TTL = timedelta(minutes=10)


def create_session_token(user_id: int, secret: str, ttl: timedelta) -> str:
    return _encode({"sub": str(user_id)}, "session", secret, ttl)


def decode_session_token(token: str, secret: str) -> int:
    claims = _decode(token, "session", secret)
    try:
        return int(claims["sub"])
    except (KeyError, ValueError) as exc:
        raise UnauthorizedError("invalid session token") from exc


def create_oauth_state(secret: str) -> tuple[str, str]:
    """(state, nonce) 를 만든다. nonce 는 브라우저 쿠키에 두고 state 안의 값과 맞춰본다."""
    nonce = secrets.token_urlsafe(16)
    return _encode({"nonce": nonce}, "oauth_state", secret, OAUTH_STATE_TTL), nonce


def verify_oauth_state(state: str, nonce: str | None, secret: str) -> None:
    claims = _decode(state, "oauth_state", secret)
    expected = claims.get("nonce")
    if (
        nonce is None
        or not isinstance(expected, str)
        or not secrets.compare_digest(expected, nonce)
    ):
        raise UnauthorizedError("oauth state mismatch")


def _encode(claims: dict[str, object], purpose: TokenPurpose, secret: str, ttl: timedelta) -> str:
    now = datetime.now(UTC)
    payload = {**claims, "purpose": purpose, "iat": now, "exp": now + ttl}
    return jwt.encode(payload, secret, algorithm=_ALGORITHM)


def _decode(token: str, purpose: TokenPurpose, secret: str) -> dict[str, Any]:
    try:
        claims: dict[str, Any] = jwt.decode(
            token, secret, algorithms=[_ALGORITHM], options={"require": ["exp", "purpose"]}
        )
    except jwt.PyJWTError as exc:
        raise UnauthorizedError("invalid or expired token") from exc
    # 세션 토큰으로 OAuth state 를 통과시키는 식의 용도 혼용을 막는다.
    if claims.get("purpose") != purpose:
        raise UnauthorizedError("invalid token purpose")
    return claims


def verify_github_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    """GitHub 웹훅의 `X-Hub-Signature-256` (`sha256=<hex>`)이 본문의 HMAC 과 같은지 본다."""
    if signature_header is None:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)
