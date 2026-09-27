"""Pre-order checklist. The whole available balance is never spent."""

from decimal import Decimal

import pytest

from trading.market import BookLevel, BookSnapshot, walk_book
from trading.risk_manager import (
    RiskInput,
    RiskManager,
    execution_prices,
    stop_loss_per_unit,
)
from tests.market_data import book_around, make_symbol


def _item(**overrides: object) -> RiskInput:
    symbol = make_symbol()
    values: dict[str, object] = {
        "equity": Decimal("1000"),
        "available_quote": Decimal("1000"),
        "profile": "medium",
        "fee_rate": Decimal("0.01"),
        "entry": Decimal("2"),
        "atr": Decimal("0.05"),
        "atr_sl_multiplier": Decimal("2"),
        "take_profit_rr": Decimal("2"),
        "symbol": symbol,
        "book": book_around(Decimal("2")),
        "open_positions": 0,
        "has_position_on_symbol": False,
        "daily_net_pnl": Decimal("0"),
        "cooldown_active": False,
        "duplicate_signal": False,
        "signal_valid": True,
        "api_ok": True,
        "market_data_ok": True,
        "data_stale": False,
        "paused": False,
        "emergency_stop": False,
        "reconciliation_blocking": False,
        "max_spread_fraction": Decimal("0.005"),
        "max_slippage_fraction": Decimal("0.003"),
        "cash_reserve_fraction": Decimal("0.02"),
    }
    values.update(overrides)
    return RiskInput(**values)  # type: ignore[arg-type]


def test_size_is_risk_based_and_leaves_cash_unspent() -> None:
    decision = RiskManager().assess(_item())
    assert decision.allowed, decision.reasons
    assert decision.funds is not None and decision.size is not None
    assert decision.funds < Decimal("1000")
    assert decision.stop is not None and decision.stop < Decimal("2")
    assert decision.take_profit is not None and decision.take_profit > Decimal("2")


def test_a_zero_reserve_still_does_not_spend_the_whole_balance() -> None:
    decision = RiskManager().assess(
        _item(
            equity=Decimal("1000000"),
            available_quote=Decimal("100"),
            cash_reserve_fraction=Decimal("0"),
        )
    )
    assert decision.allowed, decision.reasons
    assert decision.funds is not None
    assert decision.funds < Decimal("100")


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("paused", True, "paused"),
        ("emergency_stop", True, "emergency_stop"),
        ("reconciliation_blocking", True, "reconciliation"),
        ("has_position_on_symbol", True, "existing_position"),
        ("open_positions", 2, "position_limit"),
        ("daily_net_pnl", Decimal("-50"), "daily_loss"),
        ("cooldown_active", True, "cooldown"),
        ("duplicate_signal", True, "duplicate"),
        ("signal_valid", False, "signal"),
        ("api_ok", False, "api"),
        ("data_stale", True, "stale"),
        ("market_data_ok", False, "ws"),
        ("available_quote", Decimal("0"), "balance"),
    ],
)
def test_checklist_blocks(field: str, value: object, reason: str) -> None:
    decision = RiskManager().assess(_item(**{field: value}))
    assert decision.allowed is False
    assert reason in decision.reasons
    assert decision.funds is None


def test_wide_spread_thin_book_and_slippage_are_rejected() -> None:
    wide = BookSnapshot(
        bids=(BookLevel(Decimal("1"), Decimal("100")),),
        asks=(BookLevel(Decimal("1.05"), Decimal("100")),),
        is_full_snapshot=True,
    )
    spread = RiskManager().assess(_item(entry=Decimal("1.05"), book=wide))
    assert "spread" in spread.reasons

    thin = BookSnapshot(
        bids=(BookLevel(Decimal("1.99"), Decimal("1")),),
        asks=(BookLevel(Decimal("2"), Decimal("0.01")),),
        is_full_snapshot=True,
    )
    liquidity = RiskManager().assess(_item(book=thin))
    assert "liquidity" in liquidity.reasons

    stepped = BookSnapshot(
        bids=(BookLevel(Decimal("1.99"), Decimal("100")),),
        asks=(BookLevel(Decimal("2"), Decimal("0.01")), BookLevel(Decimal("3"), Decimal("1000"))),
        is_full_snapshot=True,
    )
    slip = RiskManager().assess(_item(book=stepped))
    assert "slippage" in slip.reasons


def test_increment_book_is_not_used() -> None:
    partial = BookSnapshot(
        bids=(BookLevel(Decimal("1.99"), Decimal("10")),),
        asks=(BookLevel(Decimal("2"), Decimal("10")),),
        is_full_snapshot=False,
    )
    decision = RiskManager().assess(_item(book=partial))
    assert "orderbook" in decision.reasons


def test_stop_loss_cash_includes_fees_and_expected_slippage() -> None:
    decision = RiskManager().assess(
        _item(fee_rate=Decimal("0.003"), paper_slippage_fraction=Decimal("0.001"), min_net_reward_risk=Decimal("0"))
    )
    assert decision.allowed, decision.reasons
    assert decision.size is not None and decision.stop is not None and decision.take_profit is not None
    book = book_around(Decimal("2"))
    ask = book.best_ask
    assert ask is not None
    vwap = walk_book(book.asks, decision.size)
    assert vwap is not None
    book_slip = max(Decimal("0"), (vwap - ask.price) / ask.price)
    entry_px, stop_px, _take = execution_prices(
        Decimal("2"),
        decision.stop,
        decision.take_profit,
        book_slippage=book_slip,
        paper_slippage=Decimal("0.001"),
    )
    loss = stop_loss_per_unit(entry_px, stop_px, Decimal("0.003")) * decision.size
    assert loss <= Decimal("10")
    bare = Decimal("10") / (Decimal("2") - decision.stop)
    assert decision.size < bare


def test_net_reward_below_the_threshold_skips_the_trade() -> None:
    blocked = RiskManager().assess(
        _item(paper_slippage_fraction=Decimal("0.001"), min_net_reward_risk=Decimal("1.5"))
    )
    assert blocked.allowed is False
    assert "net_rr" in blocked.reasons
    assert blocked.size is None
    wide = RiskManager().assess(
        _item(
            atr=Decimal("0.5"),
            fee_rate=Decimal("0.003"),
            paper_slippage_fraction=Decimal("0.001"),
            min_net_reward_risk=Decimal("1.5"),
        )
    )
    assert wide.allowed, wide.reasons
    assert wide.size is not None
