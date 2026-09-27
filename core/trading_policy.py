"""Client decisions that later stages must keep.

Stage 3 locked these choices. Paper trading and the strategy read them from
here and from Settings so the numbers do not drift.
"""

import re
import uuid
from decimal import Decimal
from typing import Literal, Never

ExecutionModeName = Literal["paper", "testnet", "mainnet"]

DEFAULT_ATR_SL_MULTIPLIER = Decimal("2.0")
DEFAULT_TAKE_PROFIT_RR = Decimal("2.0")
DEFAULT_TRAILING_ACTIVATION_ATR = Decimal("1.5")
DEFAULT_TRAILING_ATR_MULTIPLIER = Decimal("1.5")
DEFAULT_MIN_VOLUME_RATIO = Decimal("1.0")

EXECUTION_MODE_DEFAULT: ExecutionModeName = "paper"
ALLOW_EXCHANGE_ORDERS_DEFAULT = False
FEE_RATE_IS_FRACTION_DEFAULT = True

# Approved market entry: spend quote (USDT) and cancel the remainder.
MARKET_TIME_IN_FORCE = "IOC"
MARKET_ENTRY_FIELD = "funds"

# The bot never moves funding into trading on its own.
AUTO_TRANSFER_FUNDING_TO_TRADING = False
ALWAYS_SEND_CLIENT_ORDER_ID = True

CLIENT_ORDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _assert_never(value: Never) -> Never:
    raise AssertionError(f"unhandled value: {value}")


def exchange_orders_allowed(mode: ExecutionModeName, allow_exchange_orders: bool) -> bool:
    """PAPER never places orders. Testnet and mainnet still need the manual flag."""
    match mode:
        case "paper":
            return False
        case "testnet" | "mainnet":
            return allow_exchange_orders
        case _ as unreachable:
            _assert_never(unreachable)


def fee_rate_to_fraction(raw: Decimal | str, treat_as_fraction: bool) -> Decimal:
    """Interpret a trade-fee string.

    The approved default treats ``0.01`` as a fraction (1%). The exchange text
    does not define the unit; a later tiny testnet order is the check.
    """
    value = raw if isinstance(raw, Decimal) else Decimal(str(raw))
    if treat_as_fraction:
        return value
    return value / Decimal("100")


def generate_client_order_id() -> str:
    """32 hex characters. Store this on the trade intent before POST /api/v1/orders."""
    return uuid.uuid4().hex


def validate_client_order_id(client_order_id: str) -> str:
    if CLIENT_ORDER_ID_PATTERN.fullmatch(client_order_id) is None:
        raise ValueError("clientOrderId must be 1-64 characters from [A-Za-z0-9_-]")
    return client_order_id


def trading_balance_warning(asset: str, available: str, required: str) -> str:
    """Warn only. Funding is not transferred."""
    if AUTO_TRANSFER_FUNDING_TO_TRADING:
        raise AssertionError("automatic funding transfer is not allowed")
    return (
        f"Trading balance for {asset} is not enough "
        f"(available {available}, required {required}). "
        "The bot does not transfer funding to trading."
    )
