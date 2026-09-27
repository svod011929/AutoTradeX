"""Typed xRocket HTTP errors.

Mapping follows the problem ``type`` URI in docs/XROCKET_API.md, then the
HTTP status. Token values are stripped from messages.
"""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Mapping

from core.app_logging import redact_secrets

_SYMBOL_PROBLEMS = frozenset(
    {
        "exchange_symbol_not_found",
        "exchange_symbol_incorrect",
        "exchange_symbol_not_available_for_trading",
        "exchange_symbol_not_opened",
    }
)
_PRECISION_PROBLEMS = frozenset(
    {
        "exchange_incorrect_decimal_places",
        "exchange_incorrect_precision",
        "exchange_price_out_of_range",
        "exchange_stop_price_out_of_range",
        "exchange_size_out_of_range",
        "exchange_funds_out_of_range",
    }
)


class XRocketError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        problem_type: str | None = None,
        detail: str | None = None,
        info: dict[str, object] | None = None,
    ) -> None:
        super().__init__(redact_secrets(message, ()))
        self.status_code = status_code
        self.problem_type = problem_type
        self.detail = redact_secrets(detail, ()) if detail else None
        self.info = info or {}


class InvalidTokenError(XRocketError):
    pass


class InvalidSymbolError(XRocketError):
    pass


class PrecisionError(XRocketError):
    pass


class InsufficientBalanceError(XRocketError):
    pass


class OrderNotFoundError(XRocketError):
    pass


class DuplicateClientOrderIdError(XRocketError):
    pass


class IntervalTooBigError(XRocketError):
    def __init__(
        self,
        message: str,
        *,
        max_interval_seconds: int | None = None,
        status_code: int | None = None,
        problem_type: str | None = None,
        detail: str | None = None,
        info: dict[str, object] | None = None,
    ) -> None:
        super().__init__(
            message,
            status_code=status_code,
            problem_type=problem_type,
            detail=detail,
            info=info,
        )
        self.max_interval_seconds = max_interval_seconds


class RateLimitError(XRocketError):
    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: float | None = None,
        status_code: int | None = None,
        problem_type: str | None = None,
        detail: str | None = None,
        info: dict[str, object] | None = None,
    ) -> None:
        super().__init__(
            message,
            status_code=status_code,
            problem_type=problem_type,
            detail=detail,
            info=info,
        )
        self.retry_after_seconds = retry_after_seconds


class XRocketServerError(XRocketError):
    pass


class XRocketClientError(XRocketError):
    pass


class UnknownOrderResultError(XRocketError):
    """POST /api/v1/orders may have been accepted. Do not send it again."""


class OrdersDisabledError(XRocketError):
    pass


class UserBlockedError(XRocketError):
    pass


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Read a Retry-After delay in seconds.

    The error guide only shows this header as an example. Both delta-seconds
    and HTTP-date are accepted when the header is present.
    """
    if value is None:
        return None
    text = value.strip()
    if text == "":
        return None
    try:
        seconds = float(text)
    except ValueError:
        parsed = parsedate_to_datetime(text)
        if parsed is None:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        return max(0.0, (parsed - current).total_seconds())
    return max(0.0, seconds)


def problem_key(problem_type: str | None) -> str:
    if not problem_type:
        return ""
    return problem_type.rstrip("/").rsplit("/", 1)[-1]


def map_http_error(
    status: int,
    body: object,
    headers: Mapping[str, str] | None = None,
) -> XRocketError:
    payload = body if isinstance(body, dict) else {}
    raw_type = payload.get("type")
    problem_type = str(raw_type) if raw_type else None
    key = problem_key(problem_type)
    detail_raw = payload.get("detail") or payload.get("title") or ""
    detail = str(detail_raw)
    info_raw = payload.get("info")
    info = dict(info_raw) if isinstance(info_raw, dict) else {}
    header_map = headers or {}
    retry_after = _header(header_map, "Retry-After")
    message = detail or key or f"HTTP {status}"
    common = {
        "status_code": status,
        "problem_type": problem_type,
        "detail": detail or None,
        "info": info,
    }

    if status == 429 or key in {"rate_limit", "too_many_requests"}:
        return RateLimitError(message, retry_after_seconds=parse_retry_after(retry_after), **common)

    if status == 401 or key in {"invalid_token", "unauthorized"}:
        return InvalidTokenError(message, **common)

    if key == "user_blocked":
        return UserBlockedError(message, **common)

    if key == "exchange_client_order_id_duplicate":
        return DuplicateClientOrderIdError(message, **common)

    if key == "exchange_order_not_found":
        return OrderNotFoundError(message, **common)

    if key in _SYMBOL_PROBLEMS:
        return InvalidSymbolError(message, **common)

    if key in _PRECISION_PROBLEMS:
        return PrecisionError(message, **common)

    if key == "exchange_amount_more_than_user_balance":
        return InsufficientBalanceError(message, **common)

    if key == "exchange_too_big_interval_validation":
        return IntervalTooBigError(
            message,
            max_interval_seconds=_max_interval(info),
            **common,
        )

    if 500 <= status <= 599:
        return XRocketServerError(message, **common)

    if 400 <= status <= 499:
        return XRocketClientError(message, **common)

    return XRocketError(message, **common)


def _header(headers: Mapping[str, str], name: str) -> str | None:
    folded = name.lower()
    for key, value in headers.items():
        if key.lower() == folded:
            return value
    return None


def _max_interval(info: dict[str, object]) -> int | None:
    raw = info.get("maxIntervalInSeconds")
    if raw is None:
        return None
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        return None
    return seconds
