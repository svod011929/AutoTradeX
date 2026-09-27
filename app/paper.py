"""Paper trading on public market data.

`--paper` always uses the simulated fill path. Public candles and the order
book need no token. REST trading balances need a token, so paper equity is
the local ledger started from PAPER_STARTING_EQUITY.
"""

import asyncio
import contextlib
import logging
import signal
from pathlib import Path

from sqlalchemy import select

from core.config import Settings
from core.trading_policy import fee_rate_to_fraction
from database.integrity import check_integrity
from database.models import BotSettings, Strategy, StrategySettings, User
from database.repositories.heartbeats import HeartbeatRepository
from database.session import close_db, get_sessionmaker, init_db
from database.sqlite_utils import sqlite_file_from_url
from services.backup_service import BackupService
from services.lifecycle import ShutdownPlan, StartupPlan
from services.market_feed import PublicRestFeed
from services.notification_service import NotificationService
from services.reconciliation_service import ReconciliationService
from services.watchdog import Watchdog
from trading.order_manager import OrderManager
from trading.paper_execution import PaperExecution
from trading.position_manager import PositionManager
from trading.trading_engine import TradingEngine
from xrocket.rest_client import XRocketRestClient

logger = logging.getLogger("autotrade.paper")


async def run_paper(settings: Settings, *, cycles: int | None) -> int:
    """Boot the database, recover state, then run paper cycles. No exchange orders."""
    settings = settings.model_copy(update={"execution_mode": "paper", "allow_exchange_orders": False})
    init_db(settings)
    closed = False
    try:
        code = await _run(settings, cycles)
        closed = True
        return code
    finally:
        if not closed:
            await close_db()


async def _run(settings: Settings, cycles: int | None) -> int:
    session_factory = get_sessionmaker()
    async with session_factory() as session:
        report = await check_integrity(session)
    if not report.ok:
        logger.error("SQLite integrity_check failed: %s", " ".join(report.messages))
        database_path = sqlite_file_from_url(settings.database_url)
        if database_path is not None and database_path.is_file():
            backup = BackupService(
                database_path=database_path,
                backup_dir=Path(__file__).resolve().parent.parent / "backups",
                retention=settings.backup_retention,
            )
            backup.create_backup()
        await close_db()
        return 1

    async with session_factory() as session:
        await HeartbeatRepository().beat(session, "bootstrap", status="ok", details="paper boot")
    await _ensure_paper_user(settings)
    fee = fee_rate_to_fraction(settings.default_fee_rate, settings.fee_rate_is_fraction)
    execution = PaperExecution(fee_rate=fee, slippage_fraction=settings.paper_slippage_fraction)
    pending: dict[str, ReconciliationService] = {}

    async def _on_unknown(client_order_id: str) -> None:
        await pending["reconciliation"].on_unknown(client_order_id)

    orders = OrderManager(session_factory, execution, on_unknown=_on_unknown)
    reconciliation = ReconciliationService(session_factory, execution, orders)
    pending["reconciliation"] = reconciliation
    notifications = NotificationService(
        session_factory,
        max_attempts=settings.notification_max_attempts,
    )
    positions = PositionManager(session_factory, orders, after_commit=notifications.attempt)
    watchdog = Watchdog(session_factory)
    client = XRocketRestClient.from_settings(settings, token=None)
    await client.__aenter__()
    feed = PublicRestFeed(client, depth=settings.orderbook_depth)
    user_id = await _paper_user_id()
    engine = TradingEngine(
        session_factory=session_factory,
        settings=settings,
        user_id=user_id,
        orders=orders,
        positions=positions,
        reconciliation=reconciliation,
        watchdog=watchdog,
        feed=feed,
    )
    engine.block_entries()
    worker = asyncio.create_task(notifications.serve(watchdog=watchdog))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signame in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(signame, stop.set)

    async def _load_balance() -> None:
        logger.info("paper equity starts at %s", settings.paper_starting_equity)

    async def _load_orders() -> None:
        return None

    async def _reconcile() -> None:
        found = await reconciliation.reconcile()
        if found:
            logger.info("reconciliation matched %s order(s)", len(found))

    async def _load_positions() -> None:
        open_rows = await positions.open_positions(user_id)
        logger.info("open paper positions: %s", len(open_rows))

    async def _ws() -> None:
        logger.info("paper uses public REST polling; a private socket is not required")

    async def _allow() -> None:
        engine.allow_entries()

    async def _noop_exchange() -> None:
        return None

    try:
        await StartupPlan(
            prepare_exchange=_noop_exchange,
            load_balance=_load_balance,
            load_active_orders=_load_orders,
            reconcile=_reconcile,
            load_positions=_load_positions,
            start_websocket=_ws,
            allow_trading=_allow,
        ).run()
        logger.info("paper trading started; exchange orders stay off")
        await engine.run(cycles=cycles, stop=stop, interval_seconds=20)
    finally:
        async def _block() -> None:
            engine.block_entries()

        async def _stop_engine() -> None:
            engine.stop()

        async def _stop_ws() -> None:
            return None

        async def _stop_notes() -> None:
            await notifications.stop()
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker

        async def _close() -> None:
            await client.close()
            await close_db()

        await ShutdownPlan(
            block_new_entries=_block,
            stop_engine=_stop_engine,
            stop_websocket=_stop_ws,
            stop_notifications=_stop_notes,
            close_database=_close,
        ).run()
    return 0


async def _paper_user_id() -> int:
    factory = get_sessionmaker()
    async with factory() as session:
        user = await session.scalar(select(User).where(User.telegram_id == 0))
        if user is None:
            raise RuntimeError("paper user was not created")
        return user.id


async def _ensure_paper_user(settings: Settings) -> None:
    factory = get_sessionmaker()
    async with factory() as session:
        user = await session.scalar(select(User).where(User.telegram_id == 0))
        if user is None:
            user = User(telegram_id=0, username="paper")
            session.add(user)
            await session.flush()
        bot = await session.scalar(select(BotSettings).where(BotSettings.user_id == user.id))
        if bot is None:
            bot = BotSettings(
                user_id=user.id,
                execution_mode="paper",
                risk_profile=settings.default_risk,
                enabled_symbols=settings.default_symbols,
                timeframe=settings.default_timeframe,
                is_paused=False,
                emergency_stop=False,
                paper_cash=settings.paper_starting_equity,
            )
            session.add(bot)
        elif bot.paper_cash is None:
            bot.paper_cash = settings.paper_starting_equity
        strategy = await session.scalar(select(Strategy).where(Strategy.user_id == user.id))
        if strategy is None:
            strategy = Strategy(
                user_id=user.id,
                name="Trend Following",
                kind="trend_following",
                timeframe=settings.default_timeframe,
                is_enabled=True,
            )
            session.add(strategy)
            await session.flush()
            session.add(
                StrategySettings(
                    strategy_id=strategy.id,
                    ema_fast=20,
                    ema_slow=50,
                    rsi_period=14,
                    atr_period=14,
                    atr_sl_multiplier=settings.atr_sl_multiplier,
                    take_profit_rr=settings.take_profit_rr,
                    trailing_activation_atr=settings.trailing_activation_atr,
                    trailing_atr_multiplier=settings.trailing_atr_multiplier,
                    min_volume_ratio=settings.min_volume_ratio,
                    cooldown_minutes=settings.cooldown_minutes,
                )
            )
        await session.commit()
