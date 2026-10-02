import pytest
from cryptography.fernet import Fernet

from app.core.crypto import VariableCipher
from app.core.exceptions import NotConfiguredError, VariableDecryptionError


def test_variable_cipher_roundtrip_hides_plaintext() -> None:
    cipher = VariableCipher(Fernet.generate_key().decode())

    token = cipher.encrypt("s3cret-값")

    assert "s3cret" not in token
    assert cipher.decrypt(token) == "s3cret-값"


def test_variable_cipher_empty_value_roundtrip() -> None:
    cipher = VariableCipher(Fernet.generate_key().decode())

    assert cipher.decrypt(cipher.encrypt("")) == ""


def test_variable_cipher_invalid_key_raises_not_configured() -> None:
    with pytest.raises(NotConfiguredError):
        VariableCipher("not-a-fernet-key")


def test_variable_cipher_decrypt_with_other_key_raises_decryption_error() -> None:
    token = VariableCipher(Fernet.generate_key().decode()).encrypt("x")

    with pytest.raises(VariableDecryptionError):
        VariableCipher(Fernet.generate_key().decode()).decrypt(token)


def test_variable_cipher_decrypt_garbage_raises_decryption_error() -> None:
    with pytest.raises(VariableDecryptionError):
        VariableCipher(Fernet.generate_key().decode()).decrypt("garbage")
