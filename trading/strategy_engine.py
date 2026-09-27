"""Trend-following LONG signals on closed 15m bars.

Every buy rule has to hold at once. A low RSI on its own is not a buy.
Bars that are still open are ignored.
"""

from dataclasses import dataclass
from decimal import Decimal

from trading.indicators import atr, ema, rsi, volume_ma
from trading.market import Bar


@dataclass(frozen=True)
class StrategyParams:
    ema_fast: int = 20
    ema_slow: int = 50
    rsi_period: int = 14
    atr_period: int = 14
    volume_ma_period: int = 20
    min_volume_ratio: Decimal = Decimal("1.0")
    rsi_low: Decimal = Decimal("30")
    rsi_high: Decimal = Decimal("70")
    atr_sl_multiplier: Decimal = Decimal("2.0")
    take_profit_rr: Decimal = Decimal("2.0")


@dataclass(frozen=True)
class StrategyView:
    buy: bool
    reasons: tuple[str, ...]
    close: Decimal | None
    ema_fast: Decimal | None
    ema_slow: Decimal | None
    rsi: Decimal | None
    atr: Decimal | None
    volume: Decimal | None
    volume_ma: Decimal | None
    bar_open: object | None


def evaluate(bars: list[Bar], params: StrategyParams) -> StrategyView:
    needed = max(params.ema_slow, params.rsi_period + 1, params.atr_period + 1, params.volume_ma_period)
    if len(bars) < needed:
        return _empty(("not_enough_closed_bars",))
    closes = [bar.close for bar in bars]
    highs = [bar.high for bar in bars]
    lows = [bar.low for bar in bars]
    volumes = [bar.volume for bar in bars]
    fast = ema(closes, params.ema_fast)[-1]
    slow = ema(closes, params.ema_slow)[-1]
    rsi_value = rsi(closes, params.rsi_period)[-1]
    atr_value = atr(highs, lows, closes, params.atr_period)[-1]
    volume_average = volume_ma(volumes, params.volume_ma_period)[-1]
    price = closes[-1]
    volume = volumes[-1]
    reasons: list[str] = []
    if fast <= slow:
        reasons.append("ema_not_stacked")
    if price <= slow:
        reasons.append("price_not_above_slow_ema")
    if rsi_value <= params.rsi_low or rsi_value >= params.rsi_high:
        reasons.append("rsi_extreme")
    if volume <= volume_average * params.min_volume_ratio:
        reasons.append("volume_below_threshold")
    if atr_value <= 0:
        reasons.append("atr_not_positive")
    return StrategyView(
        buy=not reasons,
        reasons=tuple(reasons),
        close=price,
        ema_fast=fast,
        ema_slow=slow,
        rsi=rsi_value,
        atr=atr_value,
        volume=volume,
        volume_ma=volume_average,
        bar_open=bars[-1].open_time,
    )


def _empty(reasons: tuple[str, ...]) -> StrategyView:
    return StrategyView(
        buy=False,
        reasons=reasons,
        close=None,
        ema_fast=None,
        ema_slow=None,
        rsi=None,
        atr=None,
        volume=None,
        volume_ma=None,
        bar_open=None,
    )
