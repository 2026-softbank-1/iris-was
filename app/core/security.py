"""세션 토큰과 OAuth state 의 서명·검증(모두 HS256 JWT), CLI 로그인 폴링 비밀의 해시·비교."""

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt

from app.core.exceptions import UnauthorizedError

# oauth_state 는 웹 로그인, cli_oauth_state 는 CLI 로그인 승인용이다. 서로 대신 쓸 수 없다.
TokenPurpose = Literal["session", "oauth_state", "cli_oauth_state"]

_ALGORITHM = "HS256"
OAUTH_STATE_TTL = timedelta(minutes=10)


def create_session_token(user_id: int, secret: str, ttl: timedelta) -> str:
    return _encode({"sub": str(user_id)}, "session", secret, ttl)


def decode_session_token(token: str, secret: str) -> int:
    claims = _decode(token, secret, "session")
    try:
        return int(claims["sub"])
    except (KeyError, ValueError) as exc:
        raise UnauthorizedError("invalid session token") from exc


def create_oauth_state(secret: str) -> tuple[str, str]:
    """(state, nonce) 를 만든다. nonce 는 브라우저 쿠키에 두고 state 안의 값과 맞춰본다."""
    nonce = secrets.token_urlsafe(16)
    return _encode({"nonce": nonce}, "oauth_state", secret, OAUTH_STATE_TTL), nonce


@dataclass(frozen=True)
class OAuthState:
    """검증을 마친 OAuth state. cli_session_id 가 있으면 CLI 로그인 승인 흐름이다."""

    cli_session_id: str | None


def create_cli_oauth_state(cli_session_id: str, secret: str) -> tuple[str, str]:
    """CLI 로그인 승인용 (state, nonce). state 에 승인할 CLI 로그인 세션의 공개 ID 를 싣는다."""
    nonce = secrets.token_urlsafe(16)
    state = _encode(
        {"nonce": nonce, "cli_session_id": cli_session_id},
        "cli_oauth_state",
        secret,
        OAUTH_STATE_TTL,
    )
    return state, nonce


def verify_oauth_state(state: str, nonce: str | None, secret: str) -> OAuthState:
    claims = _decode(state, secret, "oauth_state", "cli_oauth_state")
    expected = claims.get("nonce")
    if (
        nonce is None
        or not isinstance(expected, str)
        or not secrets.compare_digest(expected, nonce)
    ):
        raise UnauthorizedError("oauth state mismatch")
    if claims["purpose"] == "oauth_state":
        return OAuthState(cli_session_id=None)
    cli_session_id = claims.get("cli_session_id")
    if not isinstance(cli_session_id, str):
        raise UnauthorizedError("invalid oauth state")
    return OAuthState(cli_session_id=cli_session_id)


def generate_url_token() -> str:
    """추측할 수 없는 URL-safe 무작위 값(256비트). CLI 로그인 세션의 공개 ID·폴링 비밀에 쓴다."""
    return secrets.token_urlsafe(32)


def hash_poll_secret(poll_secret: str) -> str:
    # 256비트 무작위 값이라 무차별 대입이 불가능하다. 느린 해시·salt 없이 SHA-256 으로 충분하다.
    return hashlib.sha256(poll_secret.encode()).hexdigest()


def verify_poll_secret(poll_secret: str, expected_hash: str) -> bool:
    """폴링 비밀을 해시해 저장된 해시와 상수 시간으로 비교한다."""
    return hmac.compare_digest(hash_poll_secret(poll_secret), expected_hash)


def _encode(claims: dict[str, object], purpose: TokenPurpose, secret: str, ttl: timedelta) -> str:
    now = datetime.now(UTC)
    payload = {**claims, "purpose": purpose, "iat": now, "exp": now + ttl}
    return jwt.encode(payload, secret, algorithm=_ALGORITHM)


def _decode(token: str, secret: str, *purposes: TokenPurpose) -> dict[str, Any]:
    try:
        claims: dict[str, Any] = jwt.decode(
            token, secret, algorithms=[_ALGORITHM], options={"require": ["exp", "purpose"]}
        )
    except jwt.PyJWTError as exc:
        raise UnauthorizedError("invalid or expired token") from exc
    # 세션 토큰으로 OAuth state 를 통과시키는 식의 용도 혼용을 막는다.
    if claims.get("purpose") not in purposes:
        raise UnauthorizedError("invalid token purpose")
    return claims


def verify_github_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    """GitHub 웹훅의 `X-Hub-Signature-256` (`sha256=<hex>`)이 본문의 HMAC 과 같은지 본다."""
    if signature_header is None:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)
