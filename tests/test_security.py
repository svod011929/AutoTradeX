"""Token encryption and log redaction."""

import logging

import pytest
from cryptography.fernet import Fernet

from core.app_logging import SecretRedactionFilter, redact_secrets
from core.security import (
    InvalidEncryptionKeyError,
    TokenDecryptionError,
    decrypt_secret,
    encrypt_secret,
    is_valid_fernet_key,
)


def test_encrypt_and_decrypt_roundtrip() -> None:
    key = Fernet.generate_key().decode("ascii")
    token = "xrocket-live-token-do-not-log"
    ciphertext = encrypt_secret(token, key)

    assert ciphertext != token
    assert token not in ciphertext
    assert decrypt_secret(ciphertext, key) == token


def test_ciphertext_changes_between_calls() -> None:
    key = Fernet.generate_key().decode("ascii")
    first = encrypt_secret("same-token", key)
    second = encrypt_secret("same-token", key)
    assert first != second
    assert decrypt_secret(first, key) == "same-token"
    assert decrypt_secret(second, key) == "same-token"


def test_invalid_key_and_wrong_key() -> None:
    assert is_valid_fernet_key("") is False
    assert is_valid_fernet_key("not-a-key") is False
    with pytest.raises(InvalidEncryptionKeyError):
        encrypt_secret("token", "not-a-key")

    key = Fernet.generate_key().decode("ascii")
    other = Fernet.generate_key().decode("ascii")
    ciphertext = encrypt_secret("token", key)
    with pytest.raises(TokenDecryptionError):
        decrypt_secret(ciphertext, other)


def test_redact_secrets_covers_bearer_and_assignments() -> None:
    token = "super-secret-token"
    key = "enc-key-value"
    message = (
        f"token={token} Authorization: Bearer {token} "
        f"encryption_key={key} bot_token: {token}"
    )
    redacted = redact_secrets(message, [token, key])
    assert token not in redacted
    assert key not in redacted
    assert "Bearer ***" in redacted
    assert "encryption_key=***" in redacted


def test_secrets_are_redacted_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    token = "super-secret-token"
    key = "enc-key-value"
    logger = logging.getLogger("autotrade.test.security")
    logger.setLevel(logging.INFO)
    logger.propagate = True
    redaction = SecretRedactionFilter([token, key])
    logger.addFilter(redaction)
    try:
        with caplog.at_level(logging.INFO, logger="autotrade.test.security"):
            logger.info(
                "using token %s Authorization: Bearer %s key=%s",
                token,
                token,
                key,
            )
    finally:
        logger.removeFilter(redaction)

    assert token not in caplog.text
    assert key not in caplog.text
    assert "***" in caplog.text
