import hashlib
import hmac
from datetime import timedelta

import pytest

from app.core.exceptions import UnauthorizedError
from app.core.security import (
    create_oauth_state,
    create_session_token,
    decode_session_token,
    verify_github_signature,
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


def test_github_signature_accepts_matching_hmac() -> None:
    body = b'{"zen": "ok"}'
    signature = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()

    assert verify_github_signature("s3cret", body, signature) is True


@pytest.mark.parametrize("signature", [None, "", "sha256=deadbeef", "sha1=abc"])
def test_github_signature_rejects_missing_or_wrong_value(signature: str | None) -> None:
    assert verify_github_signature("s3cret", b"{}", signature) is False


def test_github_signature_rejects_body_changed_after_signing() -> None:
    signature = "sha256=" + hmac.new(b"s3cret", b"original", hashlib.sha256).hexdigest()

    assert verify_github_signature("s3cret", b"tampered", signature) is False
