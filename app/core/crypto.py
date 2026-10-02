"""서비스 환경변수 값의 암호화·복호화. Fernet(AES-128-CBC + HMAC-SHA256)으로 한다."""

from cryptography.fernet import Fernet, InvalidToken

from app.core.exceptions import NotConfiguredError, VariableDecryptionError


class VariableCipher:
    def __init__(self, key: str) -> None:
        try:
            self._fernet = Fernet(key)
        except ValueError as exc:
            raise NotConfiguredError(
                "variables encryption key is invalid", setting="VARIABLES_ENCRYPTION_KEY"
            ) from exc

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise VariableDecryptionError("variable cannot be decrypted") from exc
