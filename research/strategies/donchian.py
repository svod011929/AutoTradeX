"""Donchian breakout with a chandelier stop and no fixed take-profit.

The channel is the highest high of the previous N bars, excluding the signal
bar. The stop is ``highest high since entry - k * ATR`` and only moves up.
An optional regime filter blocks entries and exits when the long average breaks.
"""

from decimal import Decimal

from trading.indicators import atr, sma
from trading.market import Bar

from research.strategies.base import Action, TimeframeName, bars_per_day


class DonchianStrategy:
    def __init__(
        self,
        *,
        timeframe: TimeframeName,
        channel: int,
        atr_mult: Decimal,
        use_regime: bool = False,
        regime_ma_days: int = 200,
        regime_slope_days: int = 20,
        atr_period: int = 14,
    ) -> None:
        if channel < 2 or atr_period < 2:
            raise ValueError("channel and atr_period must be at least 2")
        if atr_mult <= 0:
            raise ValueError("atr_mult must be positive")
        self.timeframe = timeframe
        self.channel = channel
        self.atr_mult = atr_mult
        self.use_regime = use_regime
        self.regime_ma_days = regime_ma_days
        self.regime_slope_days = regime_slope_days
        self.atr_period = atr_period
        self._highs: list[Decimal] = []
        self._closes: list[Decimal] = []
        self._prior: list[Decimal | None] = []
        self._atr: list[Decimal | None] = []
        self._ma: list[Decimal | None] = []

    def sync(self, bars: list[Bar]) -> None:
        highs = [bar.high for bar in bars]
        lows = [bar.low for bar in bars]
        closes = [bar.close for bar in bars]
        self._highs = highs
        self._closes = closes
        prior: list[Decimal | None] = [None] * len(bars)
        window = self.channel
        for index in range(window, len(bars)):
            prior[index] = max(highs[index - window : index])
        self._prior = prior
        aligned_atr: list[Decimal | None] = [None] * len(bars)
        for offset, value in enumerate(atr(highs, lows, closes, self.atr_period)):
            aligned_atr[offset + self.atr_period - 1] = value
        self._atr = aligned_atr
        aligned_ma: list[Decimal | None] = [None] * len(bars)
        if self.use_regime:
            period = self.regime_ma_days * bars_per_day(self.timeframe)
            for offset, value in enumerate(sma(closes, period)):
                aligned_ma[offset + period - 1] = value
        self._ma = aligned_ma

    def _regime_ok(self, index: int) -> bool:
        if not self.use_regime:
            return True
        current = self._ma[index] if index < len(self._ma) else None
        slope = self.regime_slope_days * bars_per_day(self.timeframe)
        if current is None or index < slope:
            return False
        earlier = self._ma[index - slope]
        if earlier is None:
            return False
        return self._closes[index] > current and current > earlier

    def _fresh_break(self, index: int) -> bool:
        level = self._prior[index] if index < len(self._prior) else None
        if level is None or self._closes[index] <= level:
            return False
        if index == 0:
            return True
        previous = self._prior[index - 1]
        if previous is None:
            return True
        return self._closes[index - 1] <= previous

    def _stop_from(self, index: int, held_high: Decimal) -> Decimal | None:
        width = self._atr[index] if index < len(self._atr) else None
        if width is None or width <= 0:
            return None
        return held_high - (self.atr_mult * width)

    def action(self, index: int) -> Action:
        if not self._fresh_break(index) or not self._regime_ok(index):
            return Action()
        stop = self._stop_from(index, self._highs[index])
        if stop is None:
            return Action()
        return Action(enter=True, stop=stop, resting_stop=True, risk_mode="risk")

    def on_position(self, index: int, held_high: Decimal) -> Action:
        peak = held_high if held_high > self._highs[index] else self._highs[index]
        stop = self._stop_from(index, peak)
        if self.use_regime and not self._regime_ok(index):
            return Action(exit=True, stop=stop, resting_stop=True, risk_mode="risk")
        return Action(stop=stop, resting_stop=True, risk_mode="risk")
