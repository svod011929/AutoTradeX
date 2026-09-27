"""Trend-following buys only when every rule holds on a closed bar."""

from datetime import timedelta
from decimal import Decimal

from trading.market import Bar, candle_is_closed, confirmed_closed_bars
from trading.strategy_engine import StrategyParams, evaluate
from tests.market_data import START, closed_moment, trending_bars


def test_trend_following_buys_when_every_rule_holds() -> None:
    view = evaluate(trending_bars(), StrategyParams())
    assert view.buy, view.reasons
    assert view.rsi is not None
    assert Decimal("30") < view.rsi < Decimal("70")
    assert view.volume is not None and view.volume_ma is not None
    assert view.volume > view.volume_ma


def test_a_low_rsi_alone_is_not_a_buy() -> None:
    price = Decimal("100")
    bars: list[Bar] = []
    for index in range(90):
        price -= Decimal("0.4")
        bars.append(
            Bar(
                open_time=START + timedelta(minutes=15 * index),
                open=price,
                high=price + Decimal("0.2"),
                low=price - Decimal("0.2"),
                close=price,
                volume=Decimal("20") if index == 89 else Decimal("1"),
            )
        )
    view = evaluate(bars, StrategyParams())
    assert view.buy is False
    assert view.rsi is not None and view.rsi < Decimal("30")
    assert "ema_not_stacked" in view.reasons
    assert "price_not_above_slow_ema" in view.reasons


def test_volume_must_be_above_the_average() -> None:
    bars = trending_bars()
    last = bars[-1]
    bars[-1] = Bar(last.open_time, last.open, last.high, last.low, last.close, Decimal("1"))
    view = evaluate(bars, StrategyParams())
    assert view.buy is False
    assert "volume_below_threshold" in view.reasons


def test_a_bar_closes_by_time_and_an_open_bar_is_dropped() -> None:
    bars = trending_bars()
    deadline = bars[-1].open_time + timedelta(minutes=15, seconds=5)
    assert candle_is_closed(bars[-1].open_time, "15m", deadline - timedelta(seconds=1), 5) is False
    assert candle_is_closed(bars[-1].open_time, "15m", deadline, 5) is True
    now = closed_moment(bars)
    forming = Bar(
        open_time=now,
        open=Decimal("1"),
        high=Decimal("1"),
        low=Decimal("1"),
        close=Decimal("1"),
        volume=Decimal("1000"),
    )
    kept = confirmed_closed_bars(bars + [forming], timeframe="15m", now=now, grace_seconds=5)
    assert forming not in kept
    assert len(kept) == len(bars)
    assert evaluate(kept, StrategyParams()).buy is True
