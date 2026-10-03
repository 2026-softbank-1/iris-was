import hashlib
import hmac
from datetime import timedelta

import jwt
import pytest

from app.core.exceptions import UnauthorizedError
from app.core.security import (
    create_cli_oauth_state,
    create_oauth_state,
    create_session_token,
    decode_session_token,
    generate_url_token,
    hash_poll_secret,
    verify_github_signature,
    verify_oauth_state,
    verify_poll_secret,
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


def test_web_oauth_state_has_no_cli_session() -> None:
    state, nonce = create_oauth_state(SECRET)

    assert verify_oauth_state(state, nonce, SECRET).cli_session_id is None


def test_cli_oauth_state_carries_cli_session_id() -> None:
    state, nonce = create_cli_oauth_state("public-id-1", SECRET)

    assert verify_oauth_state(state, nonce, SECRET).cli_session_id == "public-id-1"


def test_cli_oauth_state_cannot_be_used_as_session_token() -> None:
    state, _ = create_cli_oauth_state("public-id-1", SECRET)

    with pytest.raises(UnauthorizedError, match="purpose"):
        decode_session_token(state, SECRET)


def test_cli_oauth_state_with_other_nonce_raises_unauthorized() -> None:
    state, _ = create_cli_oauth_state("public-id-1", SECRET)

    with pytest.raises(UnauthorizedError, match="mismatch"):
        verify_oauth_state(state, "other-nonce", SECRET)


def test_cli_oauth_state_signed_with_other_secret_raises_unauthorized() -> None:
    state, nonce = create_cli_oauth_state("public-id-1", "another-secret-with-enough-length-0000")

    with pytest.raises(UnauthorizedError):
        verify_oauth_state(state, nonce, SECRET)


def test_cli_oauth_state_without_cli_session_id_raises_unauthorized() -> None:
    forged = jwt.encode(
        {"nonce": "n", "purpose": "cli_oauth_state", "exp": 4102444800},
        SECRET,
        algorithm="HS256",
    )

    with pytest.raises(UnauthorizedError, match="invalid oauth state"):
        verify_oauth_state(forged, "n", SECRET)


def test_poll_secret_is_stored_as_hash_and_verifies_only_the_same_secret() -> None:
    poll_secret = generate_url_token()
    stored = hash_poll_secret(poll_secret)

    assert stored != poll_secret
    assert len(stored) == 64
    assert verify_poll_secret(poll_secret, stored) is True
    assert verify_poll_secret(generate_url_token(), stored) is False


@pytest.mark.parametrize("poll_secret", ["", "비밀", "x" * 1000])
def test_poll_secret_verification_rejects_unrelated_input_without_error(poll_secret: str) -> None:
    stored = hash_poll_secret(generate_url_token())

    assert verify_poll_secret(poll_secret, stored) is False


def test_generate_url_token_is_unguessable_length_and_unique() -> None:
    tokens = {generate_url_token() for _ in range(100)}

    assert len(tokens) == 100
    assert all(len(token) >= 43 for token in tokens)  # 256비트를 base64url 로 인코딩한 길이
