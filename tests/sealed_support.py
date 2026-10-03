"""Sealed Secrets 봉인 테스트가 함께 쓰는 도우미: 시험용 controller 키 쌍과 복호화."""

import base64
import struct
from datetime import UTC, datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.x509.oid import NameOID


def make_controller_key() -> tuple[rsa.RSAPrivateKey, str]:
    """시험용 controller 키와 공개 인증서(PEM). controller 의 자체 서명 인증서와 같은 모양이다."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sealed-secret")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    return key, certificate.public_bytes(serialization.Encoding.PEM).decode()


def unseal(key: rsa.RSAPrivateKey, token: str, namespace: str, name: str) -> str:
    """controller 가 하는 복호화. label 이 봉인할 때와 다르면 ValueError 다."""
    raw = base64.b64decode(token)
    (wrapped_length,) = struct.unpack(">H", raw[:2])
    session_key = key.decrypt(
        raw[2 : 2 + wrapped_length],
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=f"{namespace}/{name}".encode(),
        ),
    )
    return AESGCM(session_key).decrypt(bytes(12), raw[2 + wrapped_length :], None).decode()
