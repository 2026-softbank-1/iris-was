import re
from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from app.clients.secret_sealer import SecretSealer
from app.core.exceptions import NotConfiguredError
from tests.sealed_support import make_controller_key, unseal

# iris-infra iris-service chart 의 values.schema.json 이 봉인 값에 요구하는 형식.
CHART_CIPHERTEXT = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")


async def test_seal_values_unseal_with_controller_key() -> None:
    key, certificate = make_controller_key()

    sealed = await SecretSealer(certificate).seal(
        "svc-12",
        "vars-r345",
        {"DATABASE_URL": "postgres://u:p@h/db", "EMPTY": "", "KOR": "한글 값"},
    )

    assert set(sealed) == {"DATABASE_URL", "EMPTY", "KOR"}
    assert {k: unseal(key, v, "svc-12", "vars-r345") for k, v in sealed.items()} == {
        "DATABASE_URL": "postgres://u:p@h/db",
        "EMPTY": "",
        "KOR": "한글 값",
    }


async def test_seal_output_has_no_plaintext_and_matches_chart_schema() -> None:
    _, certificate = make_controller_key()

    sealed = await SecretSealer(certificate).seal("svc-12", "vars-r1", {"A": "super-secret-value"})

    assert CHART_CIPHERTEXT.fullmatch(sealed["A"])
    assert "super-secret-value" not in sealed["A"]


async def test_seal_same_value_twice_gives_different_ciphertext() -> None:
    _, certificate = make_controller_key()
    sealer = SecretSealer(certificate)

    first = await sealer.seal("svc-12", "vars-r1", {"A": "same"})
    second = await sealer.seal("svc-12", "vars-r1", {"A": "same"})

    assert first["A"] != second["A"]


@pytest.mark.parametrize(
    ("namespace", "name"), [("svc-13", "vars-r1"), ("svc-12", "vars-r2"), ("svc-1", "2vars-r1")]
)
async def test_seal_cannot_be_unsealed_for_other_namespace_or_name(
    namespace: str, name: str
) -> None:
    key, certificate = make_controller_key()
    sealed = await SecretSealer(certificate).seal("svc-12", "vars-r1", {"A": "x"})

    with pytest.raises(ValueError):
        unseal(key, sealed["A"], namespace, name)


async def test_seal_cannot_be_unsealed_by_other_key() -> None:
    other_key, _ = make_controller_key()
    _, certificate = make_controller_key()
    sealed = await SecretSealer(certificate).seal("svc-12", "vars-r1", {"A": "x"})

    with pytest.raises(ValueError):
        unseal(other_key, sealed["A"], "svc-12", "vars-r1")


async def test_sealer_accepts_certificate_with_escaped_newlines() -> None:
    key, certificate = make_controller_key()

    sealer = SecretSealer(certificate.strip().replace("\n", "\\n"))

    sealed = await sealer.seal("svc-1", "vars-r1", {"A": "x"})
    assert unseal(key, sealed["A"], "svc-1", "vars-r1") == "x"


@pytest.mark.parametrize("certificate", ["", "not a certificate", "-----BEGIN CERTIFICATE-----"])
def test_sealer_invalid_certificate_raises_not_configured(certificate: str) -> None:
    with pytest.raises(NotConfiguredError):
        SecretSealer(certificate)


def test_sealer_non_rsa_certificate_raises_not_configured() -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ec")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )

    with pytest.raises(NotConfiguredError):
        SecretSealer(certificate.public_bytes(serialization.Encoding.PEM).decode())
