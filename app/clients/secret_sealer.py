"""Sealed Secrets controller 의 공개 인증서로 값을 봉인한다. `kubeseal` 과 같은 hybrid 형식이다.

값마다 AES-256-GCM 세션 키를 새로 만들고, 그 키를 controller 공개 키(RSA-OAEP, SHA-256)로 감싼다.
strict scope 라서 OAEP label 이 `{namespace}/{name}` 이다. 다른 namespace·이름으로 옮긴
SealedSecret 은 controller 가 풀지 못한다. 출력은 `[RSA 암호문 길이 2바이트][RSA 암호문][AES-GCM]`
의 base64 이고 GCM nonce 는 세션 키를 한 번만 쓰므로 0이다.
"""

import asyncio
import base64
import os
import struct
from collections.abc import Mapping

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.exceptions import NotConfiguredError

_SESSION_KEY_BYTES = 32
_ZERO_NONCE = bytes(12)


class SecretSealer:
    def __init__(self, certificate_pem: str, setting: str = "SEALED_SECRETS_CERT") -> None:
        """setting 은 인증서가 어디서 왔는지다. 잘못된 인증서일 때 오류에 남긴다."""
        # 환경변수에는 줄바꿈을 `\n` 두 글자로 적어도 된다.
        pem = certificate_pem.replace("\\n", "\n").strip().encode()
        try:
            public_key = x509.load_pem_x509_certificate(pem).public_key()
        except ValueError as exc:
            raise NotConfiguredError(
                "sealed secrets certificate is invalid", setting=setting
            ) from exc
        if not isinstance(public_key, rsa.RSAPublicKey):
            raise NotConfiguredError(
                "sealed secrets certificate must hold an RSA key", setting=setting
            )
        self._public_key = public_key

    async def seal(self, namespace: str, name: str, data: Mapping[str, str]) -> dict[str, str]:
        """변수마다 봉인한다. 같은 값도 호출할 때마다 다른 암호문이 나온다."""
        return await asyncio.to_thread(self._seal, namespace, name, data)

    def _seal(self, namespace: str, name: str, data: Mapping[str, str]) -> dict[str, str]:
        label = f"{namespace}/{name}".encode()
        return {key: self._encrypt(value.encode(), label) for key, value in data.items()}

    def _encrypt(self, plaintext: bytes, label: bytes) -> str:
        session_key = os.urandom(_SESSION_KEY_BYTES)
        wrapped_key = self._public_key.encrypt(
            session_key,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=label,
            ),
        )
        body = AESGCM(session_key).encrypt(_ZERO_NONCE, plaintext, None)
        return base64.b64encode(struct.pack(">H", len(wrapped_key)) + wrapped_key + body).decode()
