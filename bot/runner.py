"""Telegram polling plus a paper trading loop. Exchange orders stay off."""

import asyncio
import contextlib
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import select

from bot.handlers import router
from core.config import Settings
from core.trading_policy import fee_rate_to_fraction
from database.integrity import check_integrity
from database.models import BotSettings, Notification, User
from database.session import close_db, get_sessionmaker, init_db
from services.market_feed import PublicRestFeed
from services.notification_service import NotificationService
from services.reconciliation_service import ReconciliationService
from services.watchdog import Watchdog
from trading.order_manager import OrderManager
from trading.paper_execution import PaperExecution
from trading.position_manager import PositionManager
from trading.trading_engine import TradingEngine
from xrocket.rest_client import XRocketRestClient

logger = logging.getLogger("autotrade.telegram")


async def run_telegram(settings: Settings) -> int:
    token = settings.bot_token.get_secret_value().strip()
    if token == "":
        logger.error("BOT_TOKEN пуст. Укажите токен бота в окружении.")
        return 2
    settings = settings.model_copy(update={"allow_exchange_orders": False})
    init_db(settings)
    session_factory = get_sessionmaker()
    async with session_factory() as session:
        report = await check_integrity(session)
    if not report.ok:
        logger.error("SQLite integrity_check failed: %s", " ".join(report.messages))
        await close_db()
        return 1

    bot = Bot(token=token)
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(router)
    async def sender(note: Notification) -> None:
        await _send(bot, note)

    notifications = NotificationService(
        session_factory,
        max_attempts=settings.notification_max_attempts,
        sender=sender,
    )
    client = XRocketRestClient.from_settings(settings, token=None)
    await client.__aenter__()
    stop = asyncio.Event()
    worker = asyncio.create_task(notifications.serve(watchdog=Watchdog(session_factory)))
    trader = asyncio.create_task(_trade_loop(settings, client, stop, notifications))

    async def list_symbols() -> set[str]:
        rows = await client.get_symbols()
        return {row.symbol.upper() for row in rows}

    logger.info("telegram polling started; exchange orders stay off")
    try:
        await dispatcher.start_polling(bot, settings=settings, list_symbols=list_symbols)
    finally:
        stop.set()
        await notifications.stop()
        trader.cancel()
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await trader
        with contextlib.suppress(asyncio.CancelledError):
            await worker
        await client.close()
        await bot.session.close()
        await close_db()
    return 0


async def _send(bot: Bot, note: Notification) -> None:
    factory = get_sessionmaker()
    async with factory() as session:
        user = await session.get(User, note.user_id)
    if user is None or user.telegram_id <= 0:
        return
    text = note.event_type
    if note.payload_json:
        text = f"{text}\n{note.payload_json}"
    await bot.send_message(user.telegram_id, text)


async def _trade_loop(
    settings: Settings,
    client: XRocketRestClient,
    stop: asyncio.Event,
    notifications: NotificationService,
) -> None:
    feed = PublicRestFeed(client, depth=settings.orderbook_depth)
    stacks: dict[int, TradingEngine] = {}
    while not stop.is_set():
        for user_id in await _started_user_ids():
            engine = stacks.get(user_id)
            if engine is None:
                engine = _engine_for(user_id, settings, feed, notifications)
                stacks[user_id] = engine
            try:
                await engine.run_cycle()
            except Exception:
                logger.exception("paper cycle failed for user %s", user_id)
        try:
            await asyncio.wait_for(stop.wait(), timeout=20)
        except TimeoutError:
            continue


def _engine_for(
    user_id: int,
    settings: Settings,
    feed: PublicRestFeed,
    notifications: NotificationService,
) -> TradingEngine:
    factory = get_sessionmaker()
    fee = fee_rate_to_fraction(settings.default_fee_rate, settings.fee_rate_is_fraction)
    execution = PaperExecution(fee_rate=fee, slippage_fraction=settings.paper_slippage_fraction)
    holder: dict[str, ReconciliationService] = {}

    async def on_unknown(client_order_id: str) -> None:
        await holder["reconciliation"].on_unknown(client_order_id)

    orders = OrderManager(factory, execution, on_unknown=on_unknown)
    reconciliation = ReconciliationService(factory, execution, orders)
    holder["reconciliation"] = reconciliation
    positions = PositionManager(factory, orders, after_commit=notifications.attempt)
    engine = TradingEngine(
        session_factory=factory,
        settings=settings,
        user_id=user_id,
        orders=orders,
        positions=positions,
        reconciliation=reconciliation,
        watchdog=Watchdog(factory),
        feed=feed,
    )
    engine.allow_entries()
    return engine


async def _started_user_ids() -> list[int]:
    factory = get_sessionmaker()
    async with factory() as session:
        rows = list((await session.scalars(select(BotSettings.user_id))).all())
    return rows
