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
# Approved exit: sell the exact base quantity, floored to baseIncrement.
MARKET_EXIT_FIELD = "size"
MARKET_EXIT_SIDE = "sell"

# A bar is closed when its window has ended, plus this grace, and REST has the bar.
CANDLE_CLOSE_GRACE_SECONDS = 5
DEFAULT_FEE_RATE = Decimal("0.01")

# Stage 5 locked the working thresholds. ATR has no upper cap.
RSI_ENTRY_MIN = Decimal("30")
RSI_ENTRY_MAX = Decimal("70")
VOLUME_MA_PERIOD = 20
MAX_SPREAD_FRACTION = Decimal("0.005")
MAX_SLIPPAGE_FRACTION = Decimal("0.003")
PAPER_SLIPPAGE_FRACTION = Decimal("0.001")
CASH_RESERVE_FRACTION = Decimal("0.02")
ATR_HAS_UPPER_BOUND = False

PAPER_DEFAULT_SYMBOLS = ("BTC-USDT", "ETH-USDT")
TESTNET_DEFAULT_SYMBOLS = ("BTC-USDT", "ETH-USDT")
MAINNET_DEFAULT_SYMBOLS = ("TON-USDT",)
MAINNET_CONFIRM_PHRASE = "START LIVE"
# Spread, liquidity and the balance check use a full book snapshot and REST trading balance.
USE_FULL_ORDERBOOK_SNAPSHOT = True
USE_REST_TRADING_BALANCE = True

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


def default_symbols_for_mode(mode: ExecutionModeName) -> str:
    """Paper and testnet start on BTC and ETH. Mainnet starts on TON."""
    match mode:
        case "paper":
            return ",".join(PAPER_DEFAULT_SYMBOLS)
        case "testnet":
            return ",".join(TESTNET_DEFAULT_SYMBOLS)
        case "mainnet":
            return ",".join(MAINNET_DEFAULT_SYMBOLS)
        case _ as unreachable:
            _assert_never(unreachable)


def parse_symbols(raw: str) -> list[str]:
    ordered: list[str] = []
    for part in raw.split(","):
        symbol = part.strip().upper()
        if symbol and symbol not in ordered:
            ordered.append(symbol)
    return ordered


class SymbolSelection:
    """Pairs kept after a listing check, and a refusal when the start must stop."""

    def __init__(
        self,
        kept: tuple[str, ...],
        dropped: tuple[str, ...],
        warnings: tuple[str, ...],
        refusal: str | None,
    ) -> None:
        self.kept = kept
        self.dropped = dropped
        self.warnings = warnings
        self.refusal = refusal


def select_listed_symbols(requested: list[str], listed: set[str], *, mode: ExecutionModeName) -> SymbolSelection:
    """Drop pairs the exchange does not list. Refuse when nothing tradable remains.

    Mainnet also refuses when TON-USDT was requested and the exchange does not have it.
    """
    known = {item.upper() for item in listed}
    kept: list[str] = []
    dropped: list[str] = []
    warnings: list[str] = []
    for symbol in requested:
        name = symbol.strip().upper()
        if not name:
            continue
        if name in known:
            if name not in kept:
                kept.append(name)
            continue
        if name not in dropped:
            dropped.append(name)
            warnings.append(f"{name} нет в списке биржи, пара отброшена")
    refusal: str | None = None
    if mode == "mainnet" and "TON-USDT" in dropped:
        refusal = "Пара TON-USDT не найдена на бирже. Запуск mainnet отменён."
    elif not kept:
        names = ", ".join(dropped) if dropped else "список пуст"
        refusal = f"Ни одна выбранная пара не торгуется на бирже ({names}). Запуск отменён."
    return SymbolSelection(tuple(kept), tuple(dropped), tuple(warnings), refusal)


def trading_balance_warning(asset: str, available: str, required: str) -> str:
    """Warn only. Funding is not transferred."""
    if AUTO_TRANSFER_FUNDING_TO_TRADING:
        raise AssertionError("automatic funding transfer is not allowed")
    return (
        f"Trading balance for {asset} is not enough "
        f"(available {available}, required {required}). "
        "The bot does not transfer funding to trading."
    )
