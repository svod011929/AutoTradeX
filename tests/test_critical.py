"""The five failure cases the spec calls out."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Settings
from database.models import Notification, Order, Position, User
from database.session import get_sessionmaker
from services.market_feed import MarketSnapshot
from services.notification_service import NotificationService
from services.reconciliation_service import ReconciliationService
from services.watchdog import Watchdog
from trading.order_manager import OrderManager
from trading.paper_execution import PaperExecution
from trading.position_manager import PositionManager
from trading.trading_engine import TradingEngine
from tests.market_data import (
    add_daily_loss,
    add_position,
    book_around,
    closed_moment,
    make_account,
    make_symbol,
    snapshot_for,
    trending_bars,
)


class _Sender:
    def __init__(self) -> None:
        self.up = False
        self.calls = 0

    async def __call__(self, note: Notification) -> None:
        del note
        self.calls += 1
        if not self.up:
            raise RuntimeError("telegram down")


def _engine(factory, execution, user_id, settings: Settings, *, sender=None) -> tuple[TradingEngine, NotificationService]:
    holder: dict[str, ReconciliationService] = {}

    async def on_unknown(client_order_id: str) -> None:
        await holder["reconciliation"].on_unknown(client_order_id)

    orders = OrderManager(factory, execution, on_unknown=on_unknown)
    reconciliation = ReconciliationService(factory, execution, orders)
    holder["reconciliation"] = reconciliation
    notifications = NotificationService(factory, max_attempts=4, sender=sender)
    positions = PositionManager(factory, orders, after_commit=notifications.attempt)
    engine = TradingEngine(
        session_factory=factory,
        settings=settings,
        user_id=user_id,
        orders=orders,
        positions=positions,
        reconciliation=reconciliation,
        watchdog=Watchdog(factory),
    )
    return engine, notifications


@pytest.mark.asyncio
async def test_lost_buy_response_is_found_without_a_second_buy(settings) -> None:
    factory = get_sessionmaker()
    user_id = await make_account(factory)
    execution = PaperExecution(fee_rate=Decimal("0.01"), slippage_fraction=Decimal("0.001"))
    execution.lose_next_buy_response = True
    engine, _notes = _engine(factory, execution, user_id, settings)
    bars = trending_bars()
    now = closed_moment(bars)
    engine.push_snapshot(snapshot_for("TON-USDT", bars, now))
    first = await engine.run_cycle(now=now)
    assert execution.buy_calls == 1
    assert first.entries == ()
    async with factory() as session:
        unknown = await session.scalar(select(Order))
        open_rows = list((await session.scalars(select(Position).where(Position.status == "open"))).all())
    assert unknown is not None and unknown.status == "unknown"
    assert open_rows == []
    second = await engine.run_cycle(now=now)
    assert execution.buy_calls == 1
    assert second.entries == ()
    async with factory() as session:
        order = await session.scalar(select(Order))
        open_rows = list((await session.scalars(select(Position).where(Position.status == "open"))).all())
    assert order is not None and order.status == "completed"
    assert len(open_rows) == 1
    assert open_rows[0].stop_loss is not None and open_rows[0].take_profit is not None


@pytest.mark.asyncio
async def test_restart_restores_stop_monitoring(settings) -> None:
    factory = get_sessionmaker()
    user_id = await make_account(factory)
    await add_position(
        factory,
        user_id=user_id,
        symbol="TON-USDT",
        entry=Decimal("12"),
        quantity=Decimal("1"),
        stop=Decimal("10"),
        take_profit=Decimal("20"),
    )
    execution = PaperExecution(fee_rate=Decimal("0"), slippage_fraction=Decimal("0"))
    engine, _notes = _engine(factory, execution, user_id, settings)
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    engine.push_snapshot(
        MarketSnapshot(
            symbol="TON-USDT",
            bars=[],
            book=book_around(Decimal("9")),
            rules=make_symbol(),
            api_ok=True,
            stale=False,
            fetched_at=now,
        )
    )
    report = await engine.run_cycle(now=now)
    assert "sl" in report.protections
    assert execution.sell_calls == 1
    assert execution.buy_calls == 0
    async with factory() as session:
        position = await session.scalar(select(Position))
    assert position is not None and position.status == "closed"


@pytest.mark.asyncio
async def test_daily_loss_blocks_a_new_buy_and_still_closes_the_open_stop(settings) -> None:
    factory = get_sessionmaker()
    user_id = await make_account(factory, symbols="BTC-USDT,TON-USDT")
    await add_position(
        factory,
        user_id=user_id,
        symbol="BTC-USDT",
        entry=Decimal("100"),
        quantity=Decimal("0.1"),
        stop=Decimal("90"),
        take_profit=Decimal("200"),
    )
    bars = trending_bars()
    now = closed_moment(bars)
    await add_daily_loss(factory, user_id=user_id, day=now, net=Decimal("-100000"))
    execution = PaperExecution(fee_rate=Decimal("0.01"), slippage_fraction=Decimal("0.001"))
    engine, _notes = _engine(factory, execution, user_id, settings)
    engine.push_snapshot(
        MarketSnapshot(
            symbol="BTC-USDT",
            bars=[],
            book=book_around(Decimal("80")),
            rules=make_symbol("BTC-USDT"),
            api_ok=True,
            stale=False,
            fetched_at=now,
        )
    )
    engine.push_snapshot(snapshot_for("TON-USDT", bars, now))
    report = await engine.run_cycle(now=now)
    assert execution.buy_calls == 0
    assert "sl" in report.protections
    assert any("daily_loss" in item for item in report.entry_blocks)
    async with factory() as session:
        ton = await session.scalar(select(Position).where(Position.symbol == "TON-USDT"))
        btc = await session.scalar(select(Position).where(Position.symbol == "BTC-USDT"))
    assert ton is None
    assert btc is not None and btc.status == "closed"


@pytest.mark.asyncio
async def test_telegram_outage_does_not_stop_the_engine(settings) -> None:
    factory = get_sessionmaker()
    user_id = await make_account(factory)
    execution = PaperExecution(fee_rate=Decimal("0.01"), slippage_fraction=Decimal("0.001"))
    sender = _Sender()
    engine, notifications = _engine(factory, execution, user_id, settings, sender=sender)
    bars = trending_bars()
    now = closed_moment(bars)
    engine.push_snapshot(snapshot_for("TON-USDT", bars, now))
    report = await engine.run_cycle(now=now)
    assert report.entries == ("TON-USDT",)
    async with factory() as session:
        note = await session.scalar(select(Notification))
        position = await session.scalar(select(Position).where(Position.status == "open"))
    assert position is not None
    assert note is not None and note.status == "pending"
    assert sender.calls >= 1
    sender.up = True
    assert await notifications.deliver_pending() == 1
    async with factory() as session:
        note = await session.scalar(select(Notification))
    assert note is not None and note.status == "sent"


@pytest.mark.asyncio
async def test_sqlite_lock_on_order_insert_retries_without_a_second_row(settings, monkeypatch) -> None:
    factory = get_sessionmaker()
    async with factory() as session:
        session.add(User(telegram_id=21, username="daniel"))
        await session.commit()
        user = await session.scalar(select(User))
        assert user is not None
        user_id = user.id
    original = AsyncSession.commit
    calls = {"n": 0}

    async def flaky(self) -> None:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise OperationalError("INSERT", {}, Exception("database is locked"))
        await original(self)

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(AsyncSession, "commit", flaky)
    monkeypatch.setattr("database.retry.asyncio.sleep", no_wait)
    execution = PaperExecution(fee_rate=Decimal("0.01"), slippage_fraction=Decimal("0"))
    orders = OrderManager(factory, execution)
    order = await orders.submit_market_buy(
        user_id=user_id,
        symbol="TON-USDT",
        funds=Decimal("15"),
        book=book_around(Decimal("2")),
    )
    assert order.status == "completed"
    assert execution.buy_calls == 1
    async with factory() as session:
        count = await session.scalar(select(func.count()).select_from(Order))
    assert count == 1


@pytest.mark.asyncio
async def test_stale_snapshot_does_not_trade(settings) -> None:
    factory = get_sessionmaker()
    user_id = await make_account(factory)
    await add_position(
        factory,
        user_id=user_id,
        symbol="TON-USDT",
        entry=Decimal("12"),
        quantity=Decimal("1"),
        stop=Decimal("10"),
        take_profit=Decimal("20"),
    )
    execution = PaperExecution(fee_rate=Decimal("0"), slippage_fraction=Decimal("0"))
    engine, _notes = _engine(factory, execution, user_id, settings)
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    engine.push_snapshot(
        MarketSnapshot(
            symbol="TON-USDT",
            bars=[],
            book=book_around(Decimal("9")),
            rules=make_symbol(),
            api_ok=True,
            stale=True,
            fetched_at=now - timedelta(hours=1),
        )
    )
    report = await engine.run_cycle(now=now)
    assert execution.buy_calls == 0
    assert execution.sell_calls == 0
    assert "stale" in report.entry_blocks
    async with factory() as session:
        position = await session.scalar(select(Position))
    assert position is not None and position.status == "open"
