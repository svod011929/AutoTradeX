"""Fernet encryption for xRocket API tokens at rest."""

from cryptography.fernet import Fernet, InvalidToken


class InvalidEncryptionKeyError(Exception):
    """ENCRYPTION_KEY is missing or is not a Fernet key."""


class TokenDecryptionError(Exception):
    """Ciphertext cannot be decrypted with the given key."""


def is_valid_fernet_key(key: str) -> bool:
    if key.strip() == "":
        return False
    try:
        Fernet(key.encode("utf-8"))
    except (ValueError, TypeError):
        return False
    return True


def _fernet(key: str) -> Fernet:
    if not is_valid_fernet_key(key):
        raise InvalidEncryptionKeyError("ENCRYPTION_KEY must be a urlsafe base64 Fernet key")
    return Fernet(key.encode("utf-8"))


def encrypt_secret(plaintext: str, key: str) -> str:
    token = _fernet(key).encrypt(plaintext.encode("utf-8"))
    return token.decode("ascii")


def decrypt_secret(ciphertext: str, key: str) -> str:
    try:
        raw = _fernet(key).decrypt(ciphertext.encode("ascii"))
    except InvalidToken as exc:
        raise TokenDecryptionError("failed to decrypt secret") from exc
    return raw.decode("utf-8")
