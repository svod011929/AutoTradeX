"""Logging setup that redacts secrets.

The module is named app_logging so it does not shadow the stdlib logging package.
"""

import logging
import re
from collections.abc import Iterable

_AUTH_RE = re.compile(r"(?i)(authorization\s*[:=]\s*)(bearer\s+)?\S+")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+\S+")
_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?token|encryption[_-]?key|bot[_-]?token|access[_-]?token)\b(\s*[:=]\s*)\S+"
)
_REDACTED = "***"


def _redact_authorization(match: re.Match[str]) -> str:
    prefix = match.group(1)
    if match.group(2):
        return f"{prefix}Bearer {_REDACTED}"
    return f"{prefix}{_REDACTED}"


def redact_secrets(message: str, secrets: Iterable[str]) -> str:
    """Remove known secret values and credential-shaped fragments from text."""
    redacted = _AUTH_RE.sub(_redact_authorization, message)
    redacted = _BEARER_RE.sub(f"Bearer {_REDACTED}", redacted)
    redacted = _ASSIGNMENT_RE.sub(rf"\1\2{_REDACTED}", redacted)
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, _REDACTED)
    return redacted


class SecretRedactionFilter(logging.Filter):
    def __init__(self, secrets: Iterable[str] | None = None) -> None:
        super().__init__()
        self._secrets = [item for item in (secrets or []) if item]

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_secrets(record.getMessage(), self._secrets)
        record.args = ()
        if record.exc_text:
            record.exc_text = redact_secrets(record.exc_text, self._secrets)
        return True


def configure_logging(level: str, secrets: Iterable[str] | None = None) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.addFilter(SecretRedactionFilter(secrets))
    logging.basicConfig(level=level, handlers=[handler], force=True)
    logging.getLogger("autotrade").setLevel(level)
