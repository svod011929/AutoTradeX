"""Backtest metrics, cache, and the closed-candle rule."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from core.config import Settings
from core.trading_policy import DEFAULT_FEE_RATE
from trading.market import Bar
from trading.strategy_engine import StrategyParams, evaluate
from backtest.__main__ import _parser
from backtest.data import CandleCache, history_cache_dir, history_rest_url, load_history
from backtest.engine import buy_fill_price, run_backtest
from backtest.metrics import TradePoint, summarize
from tests.market_data import make_symbol, trending_bars


def test_metrics_match_a_hand_computed_series() -> None:
    points = [
        TradePoint(Decimal("10"), Decimal("12"), Decimal("2"), Decimal("1")),
        TradePoint(Decimal("-5"), Decimal("-4"), Decimal("1"), Decimal("-0.5")),
        TradePoint(Decimal("0"), Decimal("1"), Decimal("1"), Decimal("0")),
    ]
    perf = summarize(points, Decimal("100"))
    assert perf.trades == 3
    assert perf.wins == 1
    assert perf.losses == 1
    assert perf.win_rate == Decimal(1) / Decimal(3)
    assert perf.gross_pnl == Decimal("9")
    assert perf.fees == Decimal("4")
    assert perf.net_pnl == Decimal("5")
    assert perf.profit_factor == Decimal("2")
    assert perf.avg_win == Decimal("10")
    assert perf.avg_loss == Decimal("-5")
    assert perf.best == Decimal("10")
    assert perf.worst == Decimal("-5")
    assert perf.average_r == Decimal("0.5") / Decimal("3")
    assert perf.max_drawdown == Decimal("5") / Decimal("110")
    assert perf.current_drawdown == Decimal("5") / Decimal("110")
    assert perf.equity == (Decimal("100"), Decimal("110"), Decimal("105"), Decimal("105"))


def test_profit_factor_is_absent_when_there_are_no_losses() -> None:
    perf = summarize(
        [TradePoint(Decimal("3"), Decimal("4"), Decimal("1"), Decimal("1"))],
        Decimal("100"),
    )
    assert perf.profit_factor is None
    assert perf.win_rate == Decimal("1")
    assert perf.losses == 0


def test_empty_summary_keeps_the_starting_equity() -> None:
    perf = summarize([], Decimal("1000"))
    assert perf.trades == 0
    assert perf.win_rate is None
    assert perf.profit_factor is None
    assert perf.max_drawdown == Decimal("0")
    assert perf.current_drawdown == Decimal("0")
    assert perf.equity == (Decimal("1000"),)


def test_entry_ignores_the_next_bar_close(monkeypatch) -> None:
    base = trending_bars()
    nxt_open = Decimal("103")
    future_open = base[-1].open_time + timedelta(minutes=15)

    def extra(close: Decimal) -> Bar:
        return Bar(
            open_time=future_open,
            open=nxt_open,
            high=nxt_open + Decimal("0.2"),
            low=nxt_open - Decimal("0.2"),
            close=close,
            volume=Decimal("1"),
        )

    seen: list[datetime] = []

    def spy(bars: list[Bar], params: StrategyParams) -> object:
        seen.append(bars[-1].open_time)
        return evaluate(bars, params)

    monkeypatch.setattr("backtest.engine.evaluate", spy)
    settings = Settings(_env_file=None)
    symbol = make_symbol("BTC-USDT")
    calm = run_backtest(base + [extra(Decimal("100"))], symbol, settings=settings)
    seen_calm = list(seen)
    seen.clear()
    wild = run_backtest(base + [extra(Decimal("500"))], symbol, settings=settings)
    calm_buys = [item for item in calm.fills if item.side == "buy"]
    wild_buys = [item for item in wild.fills if item.side == "buy"]
    assert calm_buys and wild_buys
    assert calm_buys[0].price == wild_buys[0].price
    assert calm_buys[0].price == buy_fill_price(nxt_open)
    assert calm_buys[0].price != Decimal("500")
    assert calm_buys[0].decision_open_time == base[-1].open_time
    assert future_open not in seen_calm
    assert future_open not in seen
    assert [item.price for item in calm.fills] == [item.price for item in wild.fills]


def test_candle_cache_roundtrip(tmp_path) -> None:
    bars = trending_bars()[:5]
    CandleCache(tmp_path).save("BTC-USDT", "15min", bars)
    loaded = CandleCache(tmp_path).load("BTC-USDT", "15min")
    assert [(bar.open_time, bar.close, bar.volume) for bar in loaded] == [
        (bar.open_time, bar.close, bar.volume) for bar in bars
    ]


async def test_fresh_cache_skips_rest(tmp_path, monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    moment = now - timedelta(days=3)
    bars: list[Bar] = []
    price = Decimal("10")
    while moment <= now + timedelta(hours=1):
        bars.append(Bar(moment, price, price, price, price, Decimal("1")))
        moment += timedelta(minutes=15)
    CandleCache(tmp_path).save("BTC-USDT", "15min", bars)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("REST was called")

    monkeypatch.setattr("backtest.data.XRocketRestClient", boom)
    loaded = await load_history(
        "BTC-USDT",
        1,
        rest_url="http://unused",
        cache_dir=tmp_path,
        timeframe="15m",
    )
    assert loaded
    assert loaded[0].open_time >= now - timedelta(days=1, minutes=1)


def test_history_host_and_cache_are_split_by_env(tmp_path) -> None:
    assert history_rest_url("testnet") == "https://exchange.api.testnet.xrocket.exchange"
    assert history_rest_url("mainnet") == "https://exchange.api.xrocket.exchange"
    testnet = history_cache_dir(tmp_path, "testnet")
    mainnet = history_cache_dir(tmp_path, "mainnet")
    assert testnet != mainnet
    assert testnet.name == "testnet"
    assert mainnet.name == "mainnet"
    CandleCache(testnet).save("BTC-USDT", "15min", trending_bars()[:3])
    assert CandleCache(mainnet).load("BTC-USDT", "15min") == []
    assert len(CandleCache(testnet).load("BTC-USDT", "15min")) == 3


def test_cli_defaults_to_testnet_and_can_select_mainnet() -> None:
    args = _parser().parse_args(["--pair", "BTC-USDT"])
    assert args.env == "testnet"
    assert args.fee is None
    selected = _parser().parse_args(["--pair", "ETH-USDT", "--env", "mainnet", "--fee", "0.001"])
    assert selected.env == "mainnet"
    assert selected.fee == "0.001"


def test_fee_override_leaves_the_policy_default() -> None:
    settings = Settings(_env_file=None)
    assert settings.default_fee_rate == DEFAULT_FEE_RATE == Decimal("0.01")
    copied = settings.model_copy(update={"default_fee_rate": Decimal("0.001")})
    assert settings.default_fee_rate == Decimal("0.01")
    assert copied.default_fee_rate == Decimal("0.001")


async def test_mainnet_cache_miss_does_not_read_testnet(tmp_path, monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    moment = now - timedelta(days=3)
    bars: list[Bar] = []
    price = Decimal("10")
    while moment <= now + timedelta(hours=1):
        bars.append(Bar(moment, price, price, price, price, Decimal("1")))
        moment += timedelta(minutes=15)
    CandleCache(history_cache_dir(tmp_path, "testnet")).save("BTC-USDT", "15min", bars)
    seen: list[tuple[str, str | None]] = []

    class Boom:
        def __init__(self, url: str, token: str | None = None) -> None:
            seen.append((url, token))
            raise AssertionError("REST was called")

    monkeypatch.setattr("backtest.data.XRocketRestClient", Boom)
    with pytest.raises(AssertionError, match="REST was called"):
        await load_history(
            "BTC-USDT",
            1,
            rest_url=history_rest_url("mainnet"),
            cache_dir=history_cache_dir(tmp_path, "mainnet"),
            timeframe="15m",
        )
    assert seen == [(history_rest_url("mainnet"), None)]
