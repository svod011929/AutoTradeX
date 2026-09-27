"""Stops, trailing, and a close that commits before the notifier runs."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from database.models import DailyStat, Notification, Order, Position, Trade, User
from database.session import get_sessionmaker
from trading.order_manager import OrderManager
from trading.paper_execution import PaperExecution
from trading.position_manager import PositionManager
from tests.market_data import book_around, make_symbol


@pytest.mark.asyncio
async def test_trailing_stop_never_moves_backward_and_close_is_atomic(settings) -> None:
    del settings
    factory = get_sessionmaker()
    async with factory() as session:
        session.add(User(telegram_id=3, username="daniel"))
        await session.commit()
        user = await session.scalar(select(User))
        assert user is not None
        user_id = user.id
    execution = PaperExecution(fee_rate=Decimal("0.01"), slippage_fraction=Decimal("0"))
    orders = OrderManager(factory, execution)
    seen: dict[str, object] = {}

    async def after_commit(note_id: int) -> None:
        if "position_id" not in seen:
            return
        async with factory() as session:
            position = await session.get(Position, seen["position_id"])
            assert position is not None
            seen["status_at_notify"] = position.status
            seen["note_id"] = note_id

    manager = PositionManager(factory, orders, after_commit=after_commit)
    buy = await orders.submit_market_buy(
        user_id=user_id,
        symbol="TON-USDT",
        funds=Decimal("30"),
        book=book_around(Decimal("10")),
    )
    position = await manager.open_long(
        user_id=user_id,
        symbol="TON-USDT",
        strategy_id=None,
        quantity=Decimal("1"),
        entry_price=Decimal("10"),
        stop=Decimal("8"),
        take_profit=Decimal("20"),
        buy_order_id=buy.id,
        opened_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    seen["position_id"] = position.id
    rules = make_symbol()
    book = book_around(Decimal("12"))
    await manager.protect(
        user_id=user_id,
        symbol="TON-USDT",
        price=Decimal("12"),
        atr=Decimal("1"),
        book=book,
        symbol_rules=rules,
        activation_atr=Decimal("1.5"),
        trail_atr=Decimal("1.5"),
    )
    await manager.protect(
        user_id=user_id,
        symbol="TON-USDT",
        price=Decimal("11"),
        atr=Decimal("1"),
        book=book_around(Decimal("11")),
        symbol_rules=rules,
        activation_atr=Decimal("1.5"),
        trail_atr=Decimal("1.5"),
    )
    async with factory() as session:
        stored = await session.get(Position, position.id)
        assert stored is not None
        assert stored.peak_price == Decimal("12")
        assert stored.trailing_stop == Decimal("10.5")
    events = await manager.protect(
        user_id=user_id,
        symbol="TON-USDT",
        price=Decimal("10.5"),
        atr=Decimal("1"),
        book=book_around(Decimal("10.5")),
        symbol_rules=rules,
        activation_atr=Decimal("1.5"),
        trail_atr=Decimal("1.5"),
    )
    assert events == ["trailing"]
    assert seen["status_at_notify"] == "closed"
    async with factory() as session:
        closed = await session.get(Position, position.id)
        trade = await session.scalar(select(Trade).where(Trade.position_id == position.id))
        stats = await session.scalar(select(DailyStat).where(DailyStat.user_id == user_id))
        notes = list((await session.scalars(select(Notification).where(Notification.user_id == user_id))).all())
        sell = await session.scalar(select(Order).where(Order.side == "sell"))
    assert closed is not None and closed.status == "closed"
    assert trade is not None and trade.net_pnl is not None
    assert stats is not None and stats.trades_count == 1
    assert sell is not None and sell.status == "completed" and sell.quantity == Decimal("1")
    assert any(note.event_type == "trailing" for note in notes)


@pytest.mark.asyncio
async def test_notifier_runs_only_after_the_close_commits(settings) -> None:
    del settings
    factory = get_sessionmaker()
    async with factory() as session:
        session.add(User(telegram_id=4, username="daniel"))
        await session.commit()
        user = await session.scalar(select(User))
        assert user is not None
        user_id = user.id
    execution = PaperExecution(fee_rate=Decimal("0"), slippage_fraction=Decimal("0"))
    orders = OrderManager(factory, execution)

    armed = {"on": False}

    async def boom(_note_id: int) -> None:
        if armed["on"]:
            raise RuntimeError("telegram down")

    manager = PositionManager(factory, orders, after_commit=boom)
    buy = await orders.submit_market_buy(
        user_id=user_id,
        symbol="TON-USDT",
        funds=Decimal("10"),
        book=book_around(Decimal("10")),
    )
    position = await manager.open_long(
        user_id=user_id,
        symbol="TON-USDT",
        strategy_id=None,
        quantity=Decimal("1"),
        entry_price=Decimal("10"),
        stop=Decimal("9"),
        take_profit=Decimal("12"),
        buy_order_id=buy.id,
    )
    armed["on"] = True
    with pytest.raises(RuntimeError, match="telegram down"):
        await manager.close_long(
            position_id=position.id,
            price=Decimal("9"),
            book=book_around(Decimal("9")),
            symbol_rules=make_symbol(),
            reason="sl",
        )
    async with factory() as session:
        stored = await session.get(Position, position.id)
        trade = await session.scalar(select(Trade).where(Trade.position_id == position.id))
    assert stored is not None and stored.status == "closed"
    assert trade is not None
