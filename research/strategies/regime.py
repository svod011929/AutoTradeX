"""Stay long while price is above a long moving average and that average rises.

The average length is in days, then converted to the bar size. Exit is the
next open after a close breaks the regime. There is no intrabar stop.
"""

from decimal import Decimal

from trading.indicators import sma
from trading.market import Bar

from research.strategies.base import Action, TimeframeName, bars_per_day


class RegimeStrategy:
    def __init__(self, *, timeframe: TimeframeName, ma_days: int, slope_days: int) -> None:
        if ma_days < 2 or slope_days < 1:
            raise ValueError("ma_days and slope_days must be positive")
        self.timeframe = timeframe
        self.ma_days = ma_days
        self.slope_days = slope_days
        self._ma: list[Decimal | None] = []
        self._closes: list[Decimal] = []
        self._ready = False

    def sync(self, bars: list[Bar]) -> None:
        per_day = bars_per_day(self.timeframe)
        period = self.ma_days * per_day
        closes = [bar.close for bar in bars]
        self._closes = closes
        aligned: list[Decimal | None] = [None] * len(closes)
        for offset, value in enumerate(sma(closes, period)):
            aligned[offset + period - 1] = value
        self._ma = aligned
        self._ready = True

    def _ok(self, index: int) -> bool:
        if not self._ready or index < 0 or index >= len(self._ma):
            return False
        current = self._ma[index]
        slope_bars = self.slope_days * bars_per_day(self.timeframe)
        if current is None or index < slope_bars:
            return False
        earlier = self._ma[index - slope_bars]
        if earlier is None:
            return False
        return self._closes[index] > current and current > earlier

    def action(self, index: int) -> Action:
        if not self._ok(index):
            return Action()
        return Action(enter=True, stop=self._ma[index], resting_stop=False, risk_mode="stake")

    def on_position(self, index: int, held_high: Decimal) -> Action:
        del held_high
        if self._ok(index):
            return Action(stop=self._ma[index], resting_stop=False, risk_mode="stake")
        return Action(exit=True, risk_mode="stake")
