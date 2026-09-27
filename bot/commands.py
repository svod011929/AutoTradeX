"""Telegram commands. They read rows and set flags. They do not place orders."""

import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards import (
    MENU_TEXTS,
    main_menu,
    mode_keyboard,
    settings_keyboard,
    stop_keyboard,
)
from bot.states import Onboarding
from core.config import RiskName, Settings
from core.security import encrypt_secret
from core.trading_policy import (
    MAINNET_CONFIRM_PHRASE,
    ExecutionModeName,
    default_symbols_for_mode,
    parse_symbols,
    select_listed_symbols,
)
from database.models import (
    BotSettings,
    DailyStat,
    Heartbeat,
    Notification,
    Position,
    Strategy,
    StrategySettings,
    Trade,
    User,
    XRocketAccount,
)
from database.session import get_sessionmaker
from services.notification_service import NotificationService

from backtest.metrics import TradePoint, summarize

logger = logging.getLogger("autotrade.bot")

ORDERS_OFF = "Биржевые ордера выключены. Исполнение в этом процессе бумажное."


class Sender(Protocol):
    id: int
    username: str | None


class ChatMessage(Protocol):
    text: str | None
    from_user: Sender | None

    async def delete(self) -> None:
        """Remove the user message."""

    async def answer(self, text: str, reply_markup: object = None) -> None:
        """Send a reply."""


class FlowState(Protocol):
    async def set_state(self, state: object = None) -> None:
        """Move the dialog."""

    async def clear(self) -> None:
        """Drop the dialog."""


class ButtonPress(Protocol):
    from_user: Sender | None
    message: ChatMessage | None

    async def answer(self, text: str | None = None, **kwargs: object) -> None:
        """Acknowledge the button."""


async def process_start(message: ChatMessage, settings: Settings, state: FlowState) -> None:
    sender = _sender(message)
    if sender is None:
        await message.answer("Не вижу отправителя.")
        return
    await ensure_user(sender.id, sender.username)
    if await has_bot(sender.id):
        await state.clear()
        await message.answer(await home_text(sender.id, settings), reply_markup=main_menu())
        return
    await state.set_state(Onboarding.mode)
    await message.answer(
        "Первый запуск. Выберите режим paper или testnet. MAINNET включается отдельно, фразой START LIVE.",
        reply_markup=mode_keyboard(),
    )


async def process_menu(message: ChatMessage, settings: Settings) -> None:
    sender = _sender(message)
    label = message.text or ""
    if sender is None:
        await message.answer("Не вижу отправителя.")
        return
    if label not in MENU_TEXTS:
        await message.answer("Выберите пункт меню.", reply_markup=main_menu())
        return
    if not await has_bot(sender.id):
        await message.answer("Сначала завершите первый запуск командой /start.")
        return
    if label == "🤖 Автоторговля":
        await message.answer(await home_text(sender.id, settings), reply_markup=main_menu())
        return
    if label == "💰 Баланс":
        await message.answer(await balance_text(sender.id, settings), reply_markup=main_menu())
        return
    if label == "📊 Портфель":
        await message.answer(await portfolio_text(sender.id), reply_markup=main_menu())
        return
    if label == "📈 Статистика":
        await message.answer(await stats_text(sender.id, settings), reply_markup=main_menu())
        return
    if label == "📜 История":
        await message.answer(await history_text(sender.id), reply_markup=main_menu())
        return
    if label == "🔔 Уведомления":
        await message.answer(await notifications_text(sender.id), reply_markup=main_menu())
        return
    if label == "⚙️ Настройки":
        await message.answer(await settings_text(sender.id), reply_markup=settings_keyboard())
        return
    if label == "⏸ Пауза":
        await message.answer(await toggle_pause(sender.id, settings), reply_markup=main_menu())
        return
    if label == "🛑 STOP":
        await message.answer(
            "Аварийная остановка. Выберите, что сделать с открытыми позициями.",
            reply_markup=stop_keyboard(),
        )
        return
    await message.answer("Выберите пункт меню.", reply_markup=main_menu())


async def process_admin(message: ChatMessage, settings: Settings) -> None:
    sender = _sender(message)
    if sender is None or sender.id not in settings.admin_id_list:
        await message.answer("Команда /admin доступна только администраторам.")
        return
    await message.answer(await admin_report())


async def process_token(message: ChatMessage, settings: Settings) -> None:
    """Encrypt immediately. The user message is deleted even when encryption fails."""
    plaintext = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        logger.warning("telegram message with a token could not be deleted")
    sender = _sender(message)
    if sender is None:
        await message.answer("Не вижу отправителя.")
        return
    if plaintext == "" or plaintext == "Пропустить":
        await message.answer("Токен не сохранён.")
        return
    try:
        await save_token(
            sender.id,
            plaintext,
            settings.encryption_key.get_secret_value(),
            settings.xrocket_env,
        )
    except Exception:
        logger.exception("token encryption failed")
        await message.answer("Токен не сохранён. Отправьте его ещё раз или нажмите «Пропустить».")
        return
    await message.answer("Токен зашифрован и сохранён. Сообщение с токеном удалено.")


async def process_stop(query: ButtonPress, settings: Settings, *, close_positions: bool) -> None:
    sender = query.from_user
    if sender is None:
        await query.answer()
        return
    text = await set_emergency(sender.id, settings, close_positions=close_positions)
    await query.answer()
    if query.message is not None:
        await query.message.answer(text, reply_markup=main_menu())


async def process_clear_stop(query: ButtonPress) -> None:
    sender = query.from_user
    if sender is None:
        await query.answer()
        return
    text = await clear_emergency(sender.id)
    await query.answer()
    if query.message is not None:
        await query.message.answer(text, reply_markup=main_menu())


async def prepare_mainnet(telegram_id: int) -> str:
    """First confirmation only. The stored mode stays as it is."""
    del telegram_id
    return (
        "Переход в MAINNET ещё не записан. Режим остаётся прежним. "
        "Введите фразу START LIVE точно, как написано."
    )


async def ensure_user(telegram_id: int, username: str | None) -> int:
    factory = get_sessionmaker()
    async with factory() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            user = User(telegram_id=telegram_id, username=username)
            session.add(user)
            await session.commit()
            return user.id
        if username and user.username != username:
            user.username = username
            await session.commit()
        return user.id


async def has_bot(telegram_id: int) -> bool:
    factory = get_sessionmaker()
    async with factory() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            return False
        bot = await session.scalar(select(BotSettings).where(BotSettings.user_id == user.id))
        return bot is not None


async def home_text(telegram_id: int, settings: Settings) -> str:
    factory = get_sessionmaker()
    async with factory() as session:
        user = await _user(session, telegram_id)
        if user is None:
            return "Нажмите /start, чтобы выбрать режим."
        bot = await _bot(session, user.id)
        if bot is None:
            return "Нажмите /start, чтобы выбрать режим."
        positions = await _open_positions(session, user.id)
        today = await session.scalar(
            select(DailyStat).where(DailyStat.user_id == user.id, DailyStat.date == _today())
        )
        pnl = Decimal("0") if today is None else today.net_pnl
        names = ", ".join(item.symbol for item in positions) if positions else "нет"
        extra = ""
        if bot.close_positions_requested:
            extra = "\nЗакрытие позиций запрошено."
        return "\n".join(
            [
                f"Режим: {bot.execution_mode}",
                f"Статус: {_status(bot)}",
                ORDERS_OFF,
                f"Бумажный капитал: {_money(_equity(bot, settings))} USDT",
                f"Открытые позиции: {names}",
                f"PnL за сегодня: {_money(pnl)} USDT",
                extra,
            ]
        ).strip()


async def balance_text(telegram_id: int, settings: Settings) -> str:
    factory = get_sessionmaker()
    async with factory() as session:
        user = await _user(session, telegram_id)
        bot = None if user is None else await _bot(session, user.id)
        if user is None or bot is None:
            return "Нажмите /start, чтобы выбрать режим."
        return "\n".join(
            [
                f"Режим: {bot.execution_mode}",
                ORDERS_OFF,
                f"Бумажный капитал: {_money(_equity(bot, settings))} USDT",
                f"Стартовая сумма: {_money(bot.paper_seed if bot.paper_seed is not None else settings.paper_starting_equity)} USDT",
            ]
        )


async def portfolio_text(telegram_id: int) -> str:
    factory = get_sessionmaker()
    async with factory() as session:
        user = await _user(session, telegram_id)
        if user is None:
            return "Нажмите /start, чтобы выбрать режим."
        positions = await _open_positions(session, user.id)
        if not positions:
            return "Открытых позиций нет."
        lines = ["Открытые позиции:"]
        for item in positions:
            lines.append(
                f"{item.symbol} qty {_money(item.quantity)} вход {_money(item.entry_price)} "
                f"SL {_money(item.stop_loss)} TP {_money(item.take_profit)}"
            )
        return "\n".join(lines)


async def stats_text(telegram_id: int, settings: Settings) -> str:
    factory = get_sessionmaker()
    async with factory() as session:
        user = await _user(session, telegram_id)
        if user is None:
            return "Нажмите /start, чтобы выбрать режим."
        bot = await _bot(session, user.id)
        seed = settings.paper_starting_equity
        if bot is not None and bot.paper_seed is not None:
            seed = bot.paper_seed
        rows = list(
            (
                await session.scalars(
                    select(Trade).where(Trade.user_id == user.id).order_by(Trade.closed_at.asc())
                )
            ).all()
        )
    points = [
        TradePoint(
            net_pnl=row.net_pnl or Decimal("0"),
            gross_pnl=row.gross_pnl or Decimal("0"),
            fees=row.fees or Decimal("0"),
            r_multiple=None,
        )
        for row in rows
    ]
    perf = summarize(points, seed)
    return "\n".join(
        [
            f"Сделок: {perf.trades}",
            f"Win rate: {_pct(perf.win_rate)}",
            f"Gross PnL: {_money(perf.gross_pnl)}",
            f"Комиссии: {_money(perf.fees)}",
            f"Net PnL: {_money(perf.net_pnl)}",
            f"Средний плюс: {_opt_money(perf.avg_win)}",
            f"Средний минус: {_opt_money(perf.avg_loss)}",
            f"Profit factor: {_opt_num(perf.profit_factor)}",
            f"Макс. просадка: {_pct(perf.max_drawdown)}",
            f"Текущая просадка: {_pct(perf.current_drawdown)}",
            f"Лучшая: {_opt_money(perf.best)}",
            f"Худшая: {_opt_money(perf.worst)}",
        ]
    )


async def history_text(telegram_id: int) -> str:
    factory = get_sessionmaker()
    async with factory() as session:
        user = await _user(session, telegram_id)
        if user is None:
            return "Нажмите /start, чтобы выбрать режим."
        rows = list(
            (
                await session.scalars(
                    select(Trade).where(Trade.user_id == user.id).order_by(Trade.closed_at.desc()).limit(15)
                )
            ).all()
        )
    if not rows:
        return "Закрытых сделок пока нет."
    lines = ["Последние сделки:"]
    for row in rows:
        when = row.closed_at.isoformat() if row.closed_at is not None else ""
        lines.append(f"{when} {row.symbol} net {_money(row.net_pnl)}")
    return "\n".join(lines)


async def notifications_text(telegram_id: int) -> str:
    factory = get_sessionmaker()
    async with factory() as session:
        user = await _user(session, telegram_id)
        if user is None:
            return "Нажмите /start, чтобы выбрать режим."
        pending = await session.scalar(
            select(func.count())
            .select_from(Notification)
            .where(Notification.user_id == user.id, Notification.status == "pending")
        )
        rows = list(
            (
                await session.scalars(
                    select(Notification)
                    .where(Notification.user_id == user.id)
                    .order_by(Notification.created_at.desc())
                    .limit(10)
                )
            ).all()
        )
    lines = [f"В очереди: {pending or 0}"]
    if not rows:
        lines.append("Уведомлений пока нет.")
        return "\n".join(lines)
    for row in rows:
        lines.append(f"{row.event_type} — {row.status}")
    return "\n".join(lines)


async def settings_text(telegram_id: int) -> str:
    factory = get_sessionmaker()
    async with factory() as session:
        user = await _user(session, telegram_id)
        bot = None if user is None else await _bot(session, user.id)
        if user is None or bot is None:
            return "Нажмите /start, чтобы выбрать режим."
        return "\n".join(
            [
                f"Режим: {bot.execution_mode}",
                f"Риск: {bot.risk_profile}",
                f"Пары: {bot.enabled_symbols}",
                f"Таймфрейм: {bot.timeframe}",
                f"Пауза: {'да' if bot.is_paused else 'нет'}",
                f"Аварийная остановка: {'да' if bot.emergency_stop else 'нет'}",
                f"MAINNET подтверждён: {'да' if bot.live_confirmed else 'нет'}",
                ORDERS_OFF,
            ]
        )


async def toggle_pause(telegram_id: int, settings: Settings) -> str:
    factory = get_sessionmaker()
    async with factory() as session:
        user = await _user(session, telegram_id)
        bot = None if user is None else await _bot(session, user.id)
        if user is None or bot is None:
            return "Сначала завершите первый запуск."
        if bot.emergency_stop:
            return "Аварийная остановка включена. Сначала снимите её в настройках."
        bot.is_paused = not bot.is_paused
        paused = bot.is_paused
        user_id = user.id
        await session.commit()
    await _note(user_id, "paused" if paused else "resumed", settings)
    if paused:
        return "Пауза включена. Новые входы остановлены, открытые позиции остаются под присмотром."
    return "Торговля возобновлена."


async def set_emergency(telegram_id: int, settings: Settings, *, close_positions: bool) -> str:
    factory = get_sessionmaker()
    async with factory() as session:
        user = await _user(session, telegram_id)
        bot = None if user is None else await _bot(session, user.id)
        if user is None or bot is None:
            return "Сначала завершите первый запуск."
        bot.emergency_stop = True
        bot.close_positions_requested = close_positions
        user_id = user.id
        await session.commit()
    await _note(user_id, "emergency_stop", settings)
    if close_positions:
        return "Аварийная остановка включена. Ядро закроет позиции, когда стакан будет свежим."
    return "Аварийная остановка включена. Открытые позиции оставлены под присмотром стопов."


async def clear_emergency(telegram_id: int) -> str:
    factory = get_sessionmaker()
    async with factory() as session:
        user = await _user(session, telegram_id)
        bot = None if user is None else await _bot(session, user.id)
        if user is None or bot is None:
            return "Сначала завершите первый запуск."
        bot.emergency_stop = False
        bot.close_positions_requested = False
        await session.commit()
    return "Аварийная остановка снята. Позиции этим действием не закрываются."


async def save_token(telegram_id: int, plaintext: str, key: str, environment: str) -> None:
    ciphertext = encrypt_secret(plaintext, key)
    factory = get_sessionmaker()
    async with factory() as session:
        user = await _user(session, telegram_id)
        if user is None:
            user = User(telegram_id=telegram_id, username=None)
            session.add(user)
            await session.flush()
        account = await session.scalar(select(XRocketAccount).where(XRocketAccount.user_id == user.id))
        if account is None:
            account = XRocketAccount(
                user_id=user.id,
                encrypted_api_token=ciphertext,
                environment=environment,
                is_connected=True,
            )
            session.add(account)
        else:
            account.encrypted_api_token = ciphertext
            account.environment = environment
            account.is_connected = True
        await session.commit()


async def complete_onboarding(
    telegram_id: int,
    *,
    mode: str,
    amount: Decimal,
    risk: str,
    symbols_raw: str,
    listed: set[str],
    settings: Settings,
) -> tuple[bool, str]:
    chosen = _startup_mode(mode)
    if chosen is None:
        return False, "На первом запуске доступны paper и testnet. MAINNET включается отдельно, фразой START LIVE."
    profile = _risk_name(risk)
    if profile is None:
        return False, "Риск должен быть low, medium или high."
    if amount <= 0:
        return False, "Стартовая сумма должна быть больше нуля."
    raw = symbols_raw.strip()
    if raw == "" or raw.casefold() == "по умолчанию":
        requested = parse_symbols(default_symbols_for_mode(chosen))
    else:
        requested = parse_symbols(raw)
    selection = select_listed_symbols(requested, listed, mode=chosen)
    if selection.refusal is not None:
        return False, selection.refusal
    factory = get_sessionmaker()
    async with factory() as session:
        user = await _user(session, telegram_id)
        if user is None:
            user = User(telegram_id=telegram_id, username=None)
            session.add(user)
            await session.flush()
        account = await session.scalar(select(XRocketAccount).where(XRocketAccount.user_id == user.id))
        if account is None:
            session.add(
                XRocketAccount(
                    user_id=user.id,
                    encrypted_api_token=None,
                    environment=settings.xrocket_env,
                    is_connected=False,
                )
            )
        bot = await _bot(session, user.id)
        if bot is None:
            bot = BotSettings(user_id=user.id)
            session.add(bot)
        bot.execution_mode = chosen
        bot.risk_profile = profile
        bot.enabled_symbols = ",".join(selection.kept)
        bot.timeframe = settings.default_timeframe
        bot.is_paused = False
        bot.emergency_stop = False
        bot.live_confirmed = False
        bot.paper_seed = amount
        bot.paper_cash = amount
        bot.close_positions_requested = False
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
    lines = list(selection.warnings)
    lines.append(
        f"Запуск записан. Режим {chosen}. Риск {profile}. Пары: {', '.join(selection.kept)}. {ORDERS_OFF}"
    )
    return True, "\n".join(lines)


async def apply_mainnet_phrase(
    telegram_id: int,
    phrase: str,
    *,
    settings: Settings,
    listed: set[str],
) -> tuple[bool, str]:
    """Write MAINNET only after the exact phrase, a mainnet environment, and a listed TON-USDT."""
    if phrase != MAINNET_CONFIRM_PHRASE:
        return False, "Фраза не совпала. Режим остаётся прежним."
    if settings.xrocket_env != "mainnet":
        return False, "XROCKET_ENV должен быть mainnet. Режим остаётся прежним."
    known = {item.upper() for item in listed}
    if "TON-USDT" not in known:
        return False, "Пара TON-USDT не найдена на бирже. Запуск mainnet отменён."
    factory = get_sessionmaker()
    async with factory() as session:
        user = await _user(session, telegram_id)
        bot = None if user is None else await _bot(session, user.id)
        if user is None or bot is None:
            return False, "Сначала завершите первый запуск. Режим остаётся прежним."
        bot.execution_mode = "mainnet"
        bot.live_confirmed = True
        await session.commit()
    return True, f"Режим MAINNET записан. {ORDERS_OFF}"


async def admin_report() -> str:
    """Health lines for admins. Tokens and keys are not selected."""
    factory = get_sessionmaker()
    async with factory() as session:
        users = await session.scalar(select(func.count()).select_from(User))
        open_positions = await session.scalar(
            select(func.count()).select_from(Position).where(Position.status == "open")
        )
        pending = await session.scalar(
            select(func.count()).select_from(Notification).where(Notification.status == "pending")
        )
        bots = list((await session.scalars(select(BotSettings))).all())
        beats = list((await session.scalars(select(Heartbeat))).all())
        people = list((await session.scalars(select(User.telegram_id))).all())
    lines = [
        "Админ-сводка",
        ORDERS_OFF,
        f"Пользователей: {users or 0}",
        f"Telegram id: {', '.join(str(item) for item in people) if people else 'нет'}",
        f"Открытых позиций: {open_positions or 0}",
        f"Уведомлений в очереди: {pending or 0}",
    ]
    if not bots:
        lines.append("Настроек бота нет.")
    for bot in bots:
        lines.append(
            f"user {bot.user_id}: режим {bot.execution_mode}, пауза {bot.is_paused}, "
            f"стоп {bot.emergency_stop}, live {bot.live_confirmed}"
        )
    if not beats:
        lines.append("Heartbeats: нет")
    for beat in beats:
        lines.append(f"heartbeat {beat.service}: {beat.status}")
    return "\n".join(lines)


async def _note(user_id: int, event_type: str, settings: Settings) -> None:
    service = NotificationService(get_sessionmaker(), max_attempts=settings.notification_max_attempts)
    await service.enqueue(user_id, event_type, '{"source":"telegram"}')


def _startup_mode(value: str) -> ExecutionModeName | None:
    match value:
        case "paper":
            return "paper"
        case "testnet":
            return "testnet"
        case _:
            return None


def _risk_name(value: str) -> RiskName | None:
    match value:
        case "low":
            return "low"
        case "medium":
            return "medium"
        case "high":
            return "high"
        case _:
            return None


def _sender(message: ChatMessage) -> Sender | None:
    return message.from_user


async def _user(session: AsyncSession, telegram_id: int) -> User | None:
    return await session.scalar(select(User).where(User.telegram_id == telegram_id))


async def _bot(session: AsyncSession, user_id: int) -> BotSettings | None:
    return await session.scalar(select(BotSettings).where(BotSettings.user_id == user_id))


async def _open_positions(session: AsyncSession, user_id: int) -> list[Position]:
    return list(
        (
            await session.scalars(
                select(Position).where(Position.user_id == user_id, Position.status == "open")
            )
        ).all()
    )


def _equity(bot: BotSettings, settings: Settings) -> Decimal:
    if bot.paper_cash is not None:
        return bot.paper_cash
    if bot.paper_seed is not None:
        return bot.paper_seed
    return settings.paper_starting_equity


def _status(bot: BotSettings) -> str:
    if bot.emergency_stop:
        return "аварийная остановка"
    if bot.is_paused:
        return "пауза"
    return "работает"


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _money(value: Decimal | None) -> str:
    if value is None:
        return "0"
    return format(value.quantize(Decimal("0.0001")), "f")


def _opt_money(value: Decimal | None) -> str:
    if value is None:
        return "—"
    return _money(value)


def _opt_num(value: Decimal | None) -> str:
    if value is None:
        return "—"
    return format(value.quantize(Decimal("0.0001")), "f")


def _pct(value: Decimal | None) -> str:
    if value is None:
        return "—"
    return f"{(value * Decimal('100')).quantize(Decimal('0.01'))}%"
