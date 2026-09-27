"""No look-ahead, and conservative limit / grid fills."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from research.binance_data import open_time_to_ms
from research.costs import MAKER_FEE, taker_buy_price, taker_sell_price
from research.engine import run_strategy
from research.grid import GridParams, run_grid
from research.strategies.base import Action
from research.strategies.donchian import DonchianStrategy
from trading.market import Bar


def _bar(day: int, open_: str, high: str, low: str, close: str) -> Bar:
    return Bar(
        open_time=datetime(2020, 1, 1, tzinfo=timezone.utc) + timedelta(days=day),
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("1"),
    )


class _Scripted:
    def __init__(self, enter_at: int, stop: str, limit: str | None = None) -> None:
        self.enter_at = enter_at
        self.stop = Decimal(stop)
        self.limit = None if limit is None else Decimal(limit)
        self.seen: list[int] = []

    def sync(self, bars: list[Bar]) -> None:
        self.bars = bars

    def action(self, index: int) -> Action:
        self.seen.append(index)
        if index != self.enter_at:
            return Action()
        return Action(
            enter=True,
            stop=self.stop,
            resting_stop=True,
            limit=self.limit,
            risk_mode="stake",
        )

    def on_position(self, index: int, held_high: Decimal) -> Action:
        del index, held_high
        return Action(stop=self.stop, resting_stop=True, risk_mode="stake")


def test_market_fill_ignores_the_future_close() -> None:
    def series(future: str) -> list[Bar]:
        bars = [_bar(i, "100", "101", "99", "100") for i in range(6)]
        bars[3] = _bar(3, "100", "108", "99", "107")
        bars[4] = _bar(4, "107", future, "99", future)
        bars[5] = _bar(5, "90", "91", "80", "88")
        return bars

    left = _Scripted(2, "95")
    right = _Scripted(2, "95")
    left_result = run_strategy(series("130"), left, start=0, end=6)
    right_result = run_strategy(series("70"), right, start=0, end=6)
    assert left_result.trades[0].entry_price == taker_buy_price(Decimal("100"))
    assert left_result.trades[0].entry_price == right_result.trades[0].entry_price
    assert left_result.trades[0].exit_price == right_result.trades[0].exit_price
    assert left_result.trades[0].reason == "stop"
    assert left.seen[0] == 1
    assert 2 in left.seen
    assert 3 not in left.seen
    assert 4 not in left.seen


def test_gap_through_stop_exits_at_the_open() -> None:
    bars = [_bar(i, "110", "112", "105", "111") for i in range(4)]
    bars[3] = _bar(3, "90", "95", "85", "92")
    result = run_strategy(bars, _Scripted(1, "100"), start=0, end=4)
    trade = result.trades[0]
    assert trade.reason == "stop"
    assert trade.exit_price == taker_sell_price(Decimal("90"))


def test_same_bar_stop_beats_the_high() -> None:
    bars = [_bar(i, "100", "101", "99", "100") for i in range(3)]
    bars[2] = _bar(2, "110", "140", "90", "120")
    result = run_strategy(bars, _Scripted(1, "100"), start=0, end=3)
    trade = result.trades[0]
    assert trade.reason == "stop"
    assert trade.entry_price == taker_buy_price(Decimal("110"))
    assert trade.exit_price == taker_sell_price(Decimal("100"))
    assert trade.exit_price < trade.entry_price


def test_limit_fills_only_when_low_is_strictly_below_and_then_times_out() -> None:
    touch = [_bar(i, "110", "112", "100", "110") for i in range(4)]
    touched = run_strategy(touch, _Scripted(1, "80", "100"), start=0, end=4, execution="maker", timeout=3)
    assert touched.fills == 0
    assert touched.fill_rate == Decimal("0")

    late = [_bar(i, "110", "112", "105", "110") for i in range(6)]
    late[4] = _bar(4, "110", "112", "90", "100")
    missed = run_strategy(late, _Scripted(1, "80", "100"), start=0, end=6, execution="maker", timeout=2)
    assert missed.signals == 1
    assert missed.fills == 0

    filled_bars = [_bar(i, "110", "112", "105", "110") for i in range(4)]
    filled_bars[2] = _bar(2, "110", "112", "99.9", "108")
    filled_bars[3] = _bar(3, "70", "72", "60", "65")
    filled = run_strategy(filled_bars, _Scripted(1, "80", "100"), start=0, end=4, execution="maker", timeout=3)
    trade = filled.trades[0]
    assert trade.entry_price == Decimal("100")
    assert trade.exit_price == taker_sell_price(Decimal("70"))
    assert trade.slippage == (Decimal("70") - trade.exit_price) * trade.quantity
    entry_fee = trade.entry_price * trade.quantity * MAKER_FEE
    exit_fee = trade.exit_price * trade.quantity * Decimal("0.003")
    assert trade.fees == entry_fee + exit_fee
    assert filled.fill_rate == Decimal("1")


def test_donchian_channel_excludes_the_signal_bar_and_the_future() -> None:
    bars = [_bar(i, "10", "10", "9", "10") for i in range(12)]
    bars[6] = _bar(6, "10", "12", "9.5", "11")
    bars[11] = _bar(11, "10", "5000", "9", "10")
    strategy = DonchianStrategy(timeframe="1d", channel=3, atr_mult=Decimal("2"), atr_period=2)
    strategy.sync(bars)
    first = strategy.action(6)
    assert first.enter
    assert first.stop is not None
    bars[11] = _bar(11, "10", "11", "9", "10")
    strategy.sync(bars)
    second = strategy.action(6)
    assert second.enter
    assert second.stop == first.stop


def test_grid_step_must_clear_the_round_trip() -> None:
    with pytest.raises(ValueError):
        GridParams(timeframe="1d", step=Decimal("0.004"), max_levels=4, lookback_days=2)


def _range_bars() -> list[Bar]:
    return [
        _bar(0, "102", "103", "100", "102"),
        _bar(1, "102", "103", "100", "102"),
        _bar(2, "102", "102.5", "100.5", "102"),
        _bar(3, "102", "102.4", "101.2", "102"),
    ]


def test_grid_limit_same_bar_and_unrealized_drawdown() -> None:
    params = GridParams(timeframe="1d", step=Decimal("0.01"), max_levels=4, lookback_days=2)
    equal_low = _range_bars() + [_bar(4, "102", "102.4", "101", "102"), _bar(5, "102", "102.4", "101", "102")]
    untouched = run_grid(equal_low, params, start=0, end=len(equal_low))
    assert untouched.fills == 0
    assert untouched.trades == ()

    bought = _range_bars() + [
        _bar(4, "102", "102.2", "100.5", "101.5"),
        _bar(5, "101.2", "101.5", "100.4", "100.4"),
    ]
    held = run_grid(bought, params, start=0, end=len(bought))
    assert held.fills == 1
    assert held.trades == ()
    assert held.max_drawdown > 0
    assert held.fill_rate is not None and held.fill_rate <= 1

    wide = GridParams(timeframe="1d", step=Decimal("0.005"), max_levels=6, lookback_days=2)
    conflict_bars = [
        _bar(0, "103", "104", "100", "103"),
        _bar(1, "103", "104", "100", "103"),
        _bar(2, "103", "104", "100.2", "103"),
        _bar(3, "103", "103.5", "102.6", "103"),
        _bar(4, "103", "103.2", "102.4", "102.8"),
        _bar(5, "102.5", "104", "102", "103"),
    ]
    conflict = run_grid(conflict_bars, wide, start=0, end=len(conflict_bars))
    assert conflict.fills >= 2
    assert all(trade.reason != "grid" for trade in conflict.trades)


def test_grid_emergency_stop_exits_at_the_open() -> None:
    bars = _range_bars() + [
        _bar(4, "102", "102.2", "100.5", "101.5"),
        _bar(5, "99", "99.5", "98", "98.5"),
    ]
    result = run_grid(
        bars,
        GridParams(timeframe="1d", step=Decimal("0.01"), max_levels=4, lookback_days=2),
        start=0,
        end=len(bars),
    )
    assert len(result.trades) == 1
    assert result.trades[0].reason == "stop"
    assert result.trades[0].exit_price == taker_sell_price(Decimal("99"))


def test_grid_segment_starts_flat() -> None:
    bars = _range_bars() + [
        _bar(4, "102", "102.2", "100.5", "101.5"),
        _bar(5, "102", "102.4", "101.2", "102"),
        _bar(6, "102", "102.4", "102", "102"),
        _bar(7, "102", "102.4", "102", "102"),
        _bar(8, "102", "102.4", "102", "102"),
    ]
    quiet = run_grid(
        bars,
        GridParams(timeframe="1d", step=Decimal("0.01"), max_levels=4, lookback_days=2),
        start=6,
        end=len(bars),
    )
    assert quiet.fills == 0
    assert quiet.trades == ()
    assert quiet.ending_equity == Decimal("1000")


def test_open_time_microseconds_become_milliseconds() -> None:
    assert open_time_to_ms(1514764800000) == 1514764800000
    assert open_time_to_ms(1785542400000000) == 1785542400000
