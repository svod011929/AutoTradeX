"""xRocket-like fills for research. Market entries reuse the backtest price.

Taker fills pay 0.3% and 0.1% paper slippage. Maker fills pay 0.2% and no
entry slippage. Stops are taker. A stop inside the bar is filled at the stop;
a gap through the stop is filled at the open.
"""

from decimal import Decimal

from backtest.engine import buy_fill_price
from core.trading_policy import (
    BACKTEST_ASSUMED_SPREAD_FRACTION,
    DEFAULT_FEE_RATE,
    DEFAULT_MAKER_FEE_RATE,
    PAPER_SLIPPAGE_FRACTION,
)
from trading.market import Bar

TAKER_FEE = DEFAULT_FEE_RATE
MAKER_FEE = DEFAULT_MAKER_FEE_RATE
TAKER_SLIPPAGE = PAPER_SLIPPAGE_FRACTION
MIN_GRID_STEP = Decimal("0.005")


def taker_buy_price(raw: Decimal) -> Decimal:
    """Next-bar open adjusted by the same helper the live backtest uses."""
    return buy_fill_price(
        raw,
        spread_fraction=BACKTEST_ASSUMED_SPREAD_FRACTION,
        slippage_fraction=TAKER_SLIPPAGE,
    )


def taker_sell_price(raw: Decimal) -> Decimal:
    return raw * (Decimal("1") - TAKER_SLIPPAGE)


def stop_trigger(stop: Decimal, bar: Bar) -> Decimal | None:
    """Raw exit before taker slippage. Stop wins over any same-bar target."""
    if bar.open <= stop:
        return bar.open
    if bar.low <= stop:
        return stop
    return None


def limit_buy_fills(limit: Decimal, bar: Bar) -> bool:
    """A resting buy limit fills only when the low trades strictly through it."""
    return bar.low < limit


def limit_sell_fills(limit: Decimal, bar: Bar) -> bool:
    """A resting sell limit fills only when the high trades strictly through it."""
    return bar.high > limit
