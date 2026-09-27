"""xRocket REST and WebSocket clients."""

from xrocket.exceptions import (
    DuplicateClientOrderIdError,
    InsufficientBalanceError,
    IntervalTooBigError,
    InvalidSymbolError,
    InvalidTokenError,
    OrderNotFoundError,
    OrdersDisabledError,
    PrecisionError,
    RateLimitError,
    UnknownOrderResultError,
    UserBlockedError,
    XRocketClientError,
    XRocketError,
    XRocketServerError,
)
from xrocket.rest_client import XRocketRestClient
from xrocket.websocket_client import XRocketWebSocketClient

__all__ = [
    "DuplicateClientOrderIdError",
    "InsufficientBalanceError",
    "IntervalTooBigError",
    "InvalidSymbolError",
    "InvalidTokenError",
    "OrderNotFoundError",
    "OrdersDisabledError",
    "PrecisionError",
    "RateLimitError",
    "UnknownOrderResultError",
    "UserBlockedError",
    "XRocketClientError",
    "XRocketError",
    "XRocketRestClient",
    "XRocketServerError",
    "XRocketWebSocketClient",
]
