"""The queue retries a few times, then marks the row failed. Trading is not the sender."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from database.models import Notification, User
from database.session import get_sessionmaker
from services.lifecycle import ShutdownPlan, StartupPlan
from services.notification_service import NotificationService
from services.watchdog import Watchdog


class _Sender:
    def __init__(self, *, fail: bool) -> None:
        self.fail = fail
        self.calls = 0

    async def __call__(self, note: Notification) -> None:
        del note
        self.calls += 1
        if self.fail:
            raise RuntimeError("telegram down")


@pytest.mark.asyncio
async def test_failed_sends_stop_after_the_attempt_cap(settings) -> None:
    del settings
    factory = get_sessionmaker()
    async with factory() as session:
        session.add(User(telegram_id=11, username="daniel"))
        await session.commit()
        user = await session.scalar(select(User))
        assert user is not None
        user_id = user.id
    sender = _Sender(fail=True)
    service = NotificationService(factory, max_attempts=3, sender=sender)
    note_id = await service.enqueue(user_id, "buy", "{}")
    assert await service.deliver_pending() == 0
    assert await service.deliver_pending() == 0
    assert await service.deliver_pending() == 0
    async with factory() as session:
        note = await session.get(Notification, note_id)
    assert note is not None
    assert note.status == "failed"
    assert note.attempts == 3
    assert sender.calls == 3


@pytest.mark.asyncio
async def test_a_later_attempt_delivers_the_queued_row(settings) -> None:
    del settings
    factory = get_sessionmaker()
    async with factory() as session:
        session.add(User(telegram_id=12, username="daniel"))
        await session.commit()
        user = await session.scalar(select(User))
        assert user is not None
        user_id = user.id
    sender = _Sender(fail=True)
    service = NotificationService(factory, max_attempts=4, sender=sender)
    note_id = await service.enqueue(user_id, "buy", "{}")
    await service.deliver_pending()
    sender.fail = False
    assert await service.deliver_pending() == 1
    async with factory() as session:
        note = await session.get(Notification, note_id)
    assert note is not None
    assert note.status == "sent"
    assert note.sent is True


@pytest.mark.asyncio
async def test_startup_shutdown_order_and_watchdog(settings) -> None:
    del settings
    factory = get_sessionmaker()
    trace: list[str] = []

    def step(name: str):
        async def _run() -> None:
            trace.append(name)

        return _run

    startup = StartupPlan(
        prepare_exchange=step("exchange"),
        load_balance=step("balance"),
        load_active_orders=step("active_orders"),
        reconcile=step("reconciliation"),
        load_positions=step("positions"),
        start_websocket=step("websocket"),
        allow_trading=step("trading"),
    )
    assert await startup.run() == [
        "exchange",
        "balance",
        "active_orders",
        "reconciliation",
        "positions",
        "websocket",
        "trading",
    ]
    shutdown = ShutdownPlan(
        block_new_entries=step("block_entries"),
        stop_engine=step("engine"),
        stop_websocket=step("websocket"),
        stop_notifications=step("notifications"),
        close_database=step("database"),
    )
    assert await shutdown.run() == ["block_entries", "engine", "websocket", "notifications", "database"]
    assert shutdown.positions_closed is False

    watchdog = Watchdog(factory, stale_after_seconds=30)
    await watchdog.beat("trading")
    assert await watchdog.stale_services(["trading"]) == []
    later = datetime.now(timezone.utc) + timedelta(seconds=31)
    assert await watchdog.stale_services(["trading", "missing"], now=later) == ["trading", "missing"]
