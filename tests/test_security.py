from datetime import timedelta

import pytest

from app.core.exceptions import UnauthorizedError
from app.core.security import (
    create_oauth_state,
    create_session_token,
    decode_session_token,
    verify_oauth_state,
)

SECRET = "test-secret-with-enough-length-for-hs256"


def test_session_token_roundtrip_returns_user_id() -> None:
    token = create_session_token(7, SECRET, timedelta(minutes=5))

    assert decode_session_token(token, SECRET) == 7


def test_session_token_expired_raises_unauthorized() -> None:
    token = create_session_token(7, SECRET, timedelta(seconds=-1))

    with pytest.raises(UnauthorizedError):
        decode_session_token(token, SECRET)


def test_session_token_signed_with_other_secret_raises_unauthorized() -> None:
    token = create_session_token(7, "another-secret-with-enough-length-0000", timedelta(minutes=5))

    with pytest.raises(UnauthorizedError):
        decode_session_token(token, SECRET)


def test_oauth_state_cannot_be_used_as_session_token() -> None:
    state, _ = create_oauth_state(SECRET)

    with pytest.raises(UnauthorizedError, match="purpose"):
        decode_session_token(state, SECRET)


def test_session_token_cannot_be_used_as_oauth_state() -> None:
    token = create_session_token(7, SECRET, timedelta(minutes=5))

    with pytest.raises(UnauthorizedError, match="purpose"):
        verify_oauth_state(token, "any", SECRET)


def test_oauth_state_matches_its_nonce() -> None:
    state, nonce = create_oauth_state(SECRET)

    verify_oauth_state(state, nonce, SECRET)


@pytest.mark.parametrize("nonce", [None, "other-nonce"])
def test_oauth_state_with_missing_or_other_nonce_raises_unauthorized(nonce: str | None) -> None:
    state, _ = create_oauth_state(SECRET)

    with pytest.raises(UnauthorizedError, match="mismatch"):
        verify_oauth_state(state, nonce, SECRET)
