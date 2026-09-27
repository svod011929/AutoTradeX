"""SQLite pragmas, bounded lock retry, and write integrity."""

import asyncio
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from database.integrity import check_integrity
from database.models import Heartbeat, SystemEvent, User
from database.repositories.heartbeats import HeartbeatRepository
from database.retry import LOCK_RETRY_DELAYS_SECONDS, run_with_sqlite_lock_retry
from database.session import get_sessionmaker
from tests.conftest import make_settings


async def _pragma(session_factory, statement: str) -> object:
    async with session_factory() as session:
        result = await session.execute(text(statement))
        return result.scalar()


@pytest.mark.asyncio
async def test_wal_foreign_keys_and_busy_timeout_are_applied(settings) -> None:
    factory = get_sessionmaker()
    journal_mode = await _pragma(factory, "PRAGMA journal_mode")
    foreign_keys = await _pragma(factory, "PRAGMA foreign_keys")
    busy_timeout = await _pragma(factory, "PRAGMA busy_timeout")
    wal_autocheckpoint = await _pragma(factory, "PRAGMA wal_autocheckpoint")
    synchronous = await _pragma(factory, "PRAGMA synchronous")

    assert str(journal_mode).lower() == "wal"
    assert int(foreign_keys) == 1
    assert int(busy_timeout) == settings.sqlite_busy_timeout
    assert int(wal_autocheckpoint) == settings.sqlite_wal_autocheckpoint
    assert int(synchronous) == 1  # NORMAL


@pytest.mark.asyncio
async def test_foreign_key_violation_is_rejected(settings) -> None:
    factory = get_sessionmaker()
    async with factory() as session:
        with pytest.raises(IntegrityError, match="FOREIGN KEY"):
            await session.execute(
                text(
                    "INSERT INTO orders ("
                    "user_id, client_order_id, symbol, side, order_type, status, filled_quantity, created_at, updated_at"
                    ") VALUES ("
                    "999, 'cid-missing-user', 'BTC-USDT', 'buy', 'market', 'pending_submit', 0, "
                    "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'"
                    ")"
                )
            )

    async with factory() as session:
        report = await check_integrity(session)
    assert report.ok


@pytest.mark.asyncio
async def test_locked_retry_is_bounded() -> None:
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    calls = 0

    async def always_locked() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("database is locked")

    with pytest.raises(RuntimeError, match="database is locked"):
        await run_with_sqlite_lock_retry(always_locked, sleep=fake_sleep)

    assert delays == list(LOCK_RETRY_DELAYS_SECONDS)
    assert calls == len(LOCK_RETRY_DELAYS_SECONDS) + 1


@pytest.mark.asyncio
async def test_locked_retry_stops_when_the_lock_clears() -> None:
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    calls = 0

    async def locked_then_ok() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RuntimeError("database is locked")
        return "done"

    result = await run_with_sqlite_lock_retry(locked_then_ok, sleep=fake_sleep)
    assert result == "done"
    assert delays == [0.1, 0.25]
    assert calls == 3


@pytest.mark.asyncio
async def test_non_lock_errors_are_not_retried() -> None:
    calls = 0

    async def boom() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("disk full")

    with pytest.raises(RuntimeError, match="disk full"):
        await run_with_sqlite_lock_retry(boom)

    assert calls == 1


@pytest.mark.asyncio
async def test_concurrent_writes_do_not_corrupt(settings) -> None:
    factory = get_sessionmaker()

    async def write_one(index: int) -> None:
        async def operation() -> None:
            async with factory() as session:
                session.add(
                    SystemEvent(
                        level="info",
                        event_type="write",
                        message=f"event-{index}",
                    )
                )
                await session.commit()

        await run_with_sqlite_lock_retry(operation)

    await asyncio.gather(*(write_one(index) for index in range(30)))

    async with factory() as session:
        report = await check_integrity(session)
        count = await session.scalar(select(func.count()).select_from(SystemEvent))
    assert report.ok
    assert count == 30


@pytest.mark.asyncio
async def test_heartbeat_service_is_unique(settings) -> None:
    factory = get_sessionmaker()
    repository = HeartbeatRepository()
    async with factory() as session:
        await repository.beat(session, "trading", status="ok", details="up")
    async with factory() as session:
        await repository.beat(session, "trading", status="degraded", details="slow")
    async with factory() as session:
        row = await repository.get(session, "trading")
        total = await session.scalar(select(func.count()).select_from(Heartbeat))
    assert row is not None
    assert row.status == "degraded"
    assert row.details == "slow"
    assert total == 1


def test_alembic_upgrade_creates_tables(tmp_path: Path) -> None:
    configured = make_settings(tmp_path)
    root = Path(__file__).resolve().parent.parent
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "database" / "migrations"))
    config.set_main_option("sqlalchemy.url", configured.database_url)
    command.upgrade(config, "head")

    database_file = tmp_path / "data" / "autotrade.db"
    connection = sqlite3.connect(database_file)
    try:
        names = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        index_names = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
    finally:
        connection.close()

    expected = {
        "users",
        "xrocket_accounts",
        "strategies",
        "strategy_settings",
        "positions",
        "orders",
        "trade_signals",
        "trades",
        "daily_stats",
        "notifications",
        "bot_settings",
        "system_events",
        "heartbeats",
        "candles",
    }
    assert expected <= names
    assert "ix_orders_user_id_status" in index_names
    assert "ix_positions_user_id_status" in index_names
    assert "ix_positions_symbol_status" in index_names
    assert "ix_system_events_created_at" in index_names
    assert "uq_candles_symbol_timeframe_open_time" in index_names


@pytest.mark.asyncio
async def test_user_telegram_id_roundtrip(settings) -> None:
    factory = get_sessionmaker()
    async with factory() as session:
        session.add(User(telegram_id=1001, username="daniel"))
        await session.commit()
    async with factory() as session:
        user = await session.scalar(select(User).where(User.telegram_id == 1001))
    assert user is not None
    assert user.username == "daniel"
