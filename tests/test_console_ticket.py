from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key

from app.core.console_ticket import (
    CONSOLE_CLUSTER_AWS,
    CONSOLE_TICKET_AUDIENCE,
    CONSOLE_TICKET_ISSUER,
    ConsoleTicketSigner,
    ConsoleTicketVerifier,
)
from app.core.exceptions import ConsoleTokenExpiredError, NotConfiguredError, UnauthorizedError
from tests.fakes_console import generate_ed25519_pem_pair

SESSION_ID = "3f0c9d3a-8f1e-4d57-9c34-6a1f2b7e5d10"


def _signer_and_verifier() -> tuple[ConsoleTicketSigner, ConsoleTicketVerifier]:
    private_pem, public_pem = generate_ed25519_pem_pair()
    return ConsoleTicketSigner(private_pem), ConsoleTicketVerifier(public_pem)


def _sign(signer: ConsoleTicketSigner, now: datetime | None = None) -> str:
    token, _ = signer.sign(SESSION_ID, 7, 42, 1, CONSOLE_CLUSTER_AWS, now)
    return token


def test_verify_signed_ticket_returns_claims() -> None:
    signer, verifier = _signer_and_verifier()
    now = datetime.now(UTC)

    token, expires_at = signer.sign(SESSION_ID, 7, 42, 1, CONSOLE_CLUSTER_AWS, now)
    claims = verifier.verify(token)

    assert claims.session_id == SESSION_ID
    assert (claims.user_id, claims.service_id, claims.target_id) == (7, 42, 1)
    assert claims.namespace == "svc-42"
    assert claims.cluster == "aws"
    assert claims.expires_at == expires_at.replace(microsecond=0)
    assert expires_at - now == timedelta(seconds=60)


def test_verify_expired_ticket_raises_token_expired() -> None:
    signer, verifier = _signer_and_verifier()
    token = _sign(signer, datetime.now(UTC) - timedelta(minutes=2))

    with pytest.raises(ConsoleTokenExpiredError):
        verifier.verify(token)


def test_verify_ticket_signed_with_other_key_raises_unauthorized() -> None:
    signer, _ = _signer_and_verifier()
    _, other_verifier = _signer_and_verifier()

    with pytest.raises(UnauthorizedError):
        other_verifier.verify(_sign(signer))


def test_verify_garbage_raises_unauthorized() -> None:
    _, verifier = _signer_and_verifier()

    with pytest.raises(UnauthorizedError):
        verifier.verify("not-a-jwt")


def _forge(private_pem: str, **overrides: object) -> str:
    now = int(datetime.now(UTC).timestamp())
    payload: dict[str, object] = {
        "iss": CONSOLE_TICKET_ISSUER,
        "aud": CONSOLE_TICKET_AUDIENCE,
        "sub": "7",
        "jti": SESSION_ID,
        "iat": now,
        "exp": now + 60,
        "svc": 42,
        "tid": 1,
        "ns": "svc-42",
        "cluster": "aws",
    }
    payload.update(overrides)
    return jwt.encode(payload, private_pem, algorithm="EdDSA")


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "someone-else"},
        {"iss": "someone-else"},
        {"exp": int(datetime.now(UTC).timestamp()) + 3600},
        {"ns": "svc-43"},
        {"svc": "not-a-number"},
    ],
    ids=["audience", "issuer", "lifetime", "namespace", "service-id"],
)
def test_verify_forged_claims_raises_unauthorized(overrides: dict[str, object]) -> None:
    private_pem, public_pem = generate_ed25519_pem_pair()

    with pytest.raises(UnauthorizedError):
        ConsoleTicketVerifier(public_pem).verify(_forge(private_pem, **overrides))


def test_verify_missing_claim_raises_unauthorized() -> None:
    private_pem, public_pem = generate_ed25519_pem_pair()
    now = int(datetime.now(UTC).timestamp())
    token = jwt.encode(
        {"iss": CONSOLE_TICKET_ISSUER, "aud": CONSOLE_TICKET_AUDIENCE, "exp": now + 60},
        private_pem,
        algorithm="EdDSA",
    )

    with pytest.raises(UnauthorizedError):
        ConsoleTicketVerifier(public_pem).verify(token)


def test_verify_hs256_token_signed_with_public_key_is_rejected() -> None:
    """알고리즘 혼동 공격: 공개키를 HMAC 비밀로 쓴 토큰은 받지 않는다."""
    _, public_pem = generate_ed25519_pem_pair()
    now = int(datetime.now(UTC).timestamp())
    token = jwt.encode(
        {
            "iss": CONSOLE_TICKET_ISSUER,
            "aud": CONSOLE_TICKET_AUDIENCE,
            "sub": "7",
            "jti": SESSION_ID,
            "iat": now,
            "exp": now + 60,
            "svc": 42,
            "tid": 1,
            "ns": "svc-42",
            "cluster": "aws",
        },
        "x" * 32,
        algorithm="HS256",
    )

    with pytest.raises(UnauthorizedError):
        ConsoleTicketVerifier(public_pem).verify(token)


def test_signer_accepts_pem_with_escaped_newlines() -> None:
    private_pem, public_pem = generate_ed25519_pem_pair()

    signer = ConsoleTicketSigner(private_pem.strip().replace("\n", "\\n"))
    verifier = ConsoleTicketVerifier(public_pem.strip().replace("\n", "\\n"))

    assert verifier.verify(_sign(signer)).session_id == SESSION_ID


def test_signer_rejects_non_ed25519_key() -> None:
    rsa_pem = (
        generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )

    with pytest.raises(NotConfiguredError):
        ConsoleTicketSigner(rsa_pem)


@pytest.mark.parametrize("pem", ["", "garbage", "-----BEGIN PRIVATE KEY-----\nAAAA\n"])
def test_signer_rejects_invalid_pem(pem: str) -> None:
    with pytest.raises(NotConfiguredError):
        ConsoleTicketSigner(pem)


def test_verifier_rejects_private_key_pem() -> None:
    private_pem = (
        Ed25519PrivateKey.generate()
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )

    with pytest.raises(NotConfiguredError):
        ConsoleTicketVerifier(private_pem)
