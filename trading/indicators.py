"""EMA, RSI, ATR, and a simple volume average.

Wilder smoothing is used for RSI and ATR. The first EMA value is the simple
average of the first window. Results are aligned to the end of the input:
the last item describes the last bar.
"""

from collections.abc import Sequence
from decimal import Decimal

NumberList = Sequence[Decimal]


def _require_period(period: int) -> None:
    if period < 1:
        raise ValueError("period must be >= 1")


def sma(values: NumberList, period: int) -> list[Decimal]:
    _require_period(period)
    if len(values) < period:
        return []
    total = sum(values[:period], Decimal("0"))
    out = [total / Decimal(period)]
    for index in range(period, len(values)):
        total += values[index] - values[index - period]
        out.append(total / Decimal(period))
    return out


def ema(values: NumberList, period: int) -> list[Decimal]:
    _require_period(period)
    if len(values) < period:
        return []
    weight = Decimal(2) / Decimal(period + 1)
    previous = sum(values[:period], Decimal("0")) / Decimal(period)
    out = [previous]
    for price in values[period:]:
        previous = (price - previous) * weight + previous
        out.append(previous)
    return out


def rsi(closes: NumberList, period: int) -> list[Decimal]:
    """Wilder RSI. Needs ``period + 1`` closes for the first value."""
    _require_period(period)
    if len(closes) <= period:
        return []
    gains: list[Decimal] = []
    losses: list[Decimal] = []
    for index in range(1, len(closes)):
        change = closes[index] - closes[index - 1]
        gains.append(change if change > 0 else Decimal("0"))
        losses.append(-change if change < 0 else Decimal("0"))
    average_gain = sum(gains[:period], Decimal("0")) / Decimal(period)
    average_loss = sum(losses[:period], Decimal("0")) / Decimal(period)
    out = [_rsi_from_averages(average_gain, average_loss)]
    for index in range(period, len(gains)):
        average_gain = (average_gain * Decimal(period - 1) + gains[index]) / Decimal(period)
        average_loss = (average_loss * Decimal(period - 1) + losses[index]) / Decimal(period)
        out.append(_rsi_from_averages(average_gain, average_loss))
    return out


def _rsi_from_averages(average_gain: Decimal, average_loss: Decimal) -> Decimal:
    if average_loss == 0 and average_gain == 0:
        return Decimal("50")
    if average_loss == 0:
        return Decimal("100")
    relative = average_gain / average_loss
    return Decimal("100") - (Decimal("100") / (Decimal("1") + relative))


def true_ranges(highs: NumberList, lows: NumberList, closes: NumberList) -> list[Decimal]:
    if not (len(highs) == len(lows) == len(closes)) or not closes:
        raise ValueError("high, low, and close must be the same non-empty length")
    ranges = [highs[0] - lows[0]]
    for index in range(1, len(closes)):
        ranges.append(
            max(
                highs[index] - lows[index],
                abs(highs[index] - closes[index - 1]),
                abs(lows[index] - closes[index - 1]),
            )
        )
    return ranges


def atr(highs: NumberList, lows: NumberList, closes: NumberList, period: int) -> list[Decimal]:
    """Wilder ATR. The first value is the simple average of the first true ranges."""
    _require_period(period)
    ranges = true_ranges(highs, lows, closes)
    if len(ranges) < period:
        return []
    previous = sum(ranges[:period], Decimal("0")) / Decimal(period)
    out = [previous]
    for true_range in ranges[period:]:
        previous = (previous * Decimal(period - 1) + true_range) / Decimal(period)
        out.append(previous)
    return out


def volume_ma(volumes: NumberList, period: int) -> list[Decimal]:
    return sma(volumes, period)
