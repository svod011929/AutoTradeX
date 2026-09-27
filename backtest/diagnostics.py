"""Read-only comparisons around a finished candle series.

These numbers are not used by the strategy or the risk manager.
"""

from dataclasses import dataclass
from decimal import Decimal

from core.config import Settings
from trading.market import Bar
from trading.strategy_engine import evaluate

from backtest.engine import _params


@dataclass(frozen=True)
class HoldResult:
    ending_equity: Decimal
    net_pnl: Decimal
    price_change: Decimal | None


def buy_and_hold(bars: list[Bar], starting_equity: Decimal, fee_rate: Decimal) -> HoldResult:
    """Buy the first close with the whole stake and sell the last close.

    The same fee fraction is charged on the way in and on the way out.
    """
    ordered = sorted(bars, key=lambda bar: bar.open_time)
    if len(ordered) < 2 or ordered[0].close <= 0:
        return HoldResult(starting_equity, Decimal("0"), None)
    first = ordered[0].close
    last = ordered[-1].close
    quantity = starting_equity / (first * (Decimal("1") + fee_rate))
    ending = quantity * last * (Decimal("1") - fee_rate)
    return HoldResult(ending, ending - starting_equity, (last - first) / first)


def entry_block_counts(bars: list[Bar], settings: Settings) -> dict[str, int]:
    """How often each strategy filter failed on a closed bar. A buy window counts as buy."""
    params = _params(settings)
    ordered = sorted(bars, key=lambda bar: bar.open_time)
    counts: dict[str, int] = {}
    for index in range(len(ordered) - 1):
        view = evaluate(ordered[: index + 1], params)
        if view.buy:
            counts["buy"] = counts.get("buy", 0) + 1
            continue
        for reason in view.reasons:
            if reason == "not_enough_closed_bars":
                continue
            counts[reason] = counts.get(reason, 0) + 1
    return counts
