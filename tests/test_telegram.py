"""Telegram commands stay out of the trading core. Orders are flags, not calls."""

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from pydantic import SecretStr
from sqlalchemy import select

from bot.commands import (
    apply_mainnet_phrase,
    has_bot,
    prepare_mainnet,
    process_admin,
    process_menu,
    process_start,
    process_stop,
    process_token,
    toggle_pause,
)
from bot.handlers import on_admin, on_launch, on_mainnet, on_mainnet_phrase, on_token
from bot.states import MainnetGate, Onboarding
from core.security import decrypt_secret
from database.models import BotSettings, Heartbeat, Notification, Order, Position, User, XRocketAccount
from database.session import get_sessionmaker
from services.notification_service import NotificationService
from services.reconciliation_service import ReconciliationService
from services.watchdog import Watchdog
from trading.order_manager import OrderManager
from trading.paper_execution import PaperExecution
from trading.position_manager import PositionManager
from trading.trading_engine import TradingEngine
from services.market_feed import MarketSnapshot
from tests.market_data import add_position, book_around, make_account, make_symbol


class FakeMessage:
    def __init__(self, text: str, user_id: int) -> None:
        self.text = text
        self.from_user = SimpleNamespace(id=user_id, username="daniel")
        self.deleted = False
        self.answers: list[str] = []

    async def delete(self) -> None:
        self.deleted = True

    async def answer(self, text: str, reply_markup: object = None) -> None:
        del reply_markup
        self.answers.append(text)


class FakeState:
    def __init__(self) -> None:
        self.state = None
        self.data: dict[str, object] = {}

    async def set_state(self, state: object = None) -> None:
        self.state = state

    async def clear(self) -> None:
        self.state = None
        self.data.clear()

    async def update_data(self, **kwargs: object) -> None:
        self.data.update(kwargs)

    async def get_data(self) -> dict[str, object]:
        return dict(self.data)


class FakeCallback:
    def __init__(self, user_id: int) -> None:
        self.from_user = SimpleNamespace(id=user_id, username="daniel")
        self.message = FakeMessage("", user_id)
        self.answered = False

    async def answer(self, text: str | None = None, **kwargs: object) -> None:
        del kwargs
        self.answered = True
        if text:
            self.message.answers.append(text)


def _engine(factory, execution, user_id, settings) -> TradingEngine:
    holder: dict[str, ReconciliationService] = {}

    async def on_unknown(client_order_id: str) -> None:
        await holder["reconciliation"].on_unknown(client_order_id)

    orders = OrderManager(factory, execution, on_unknown=on_unknown)
    reconciliation = ReconciliationService(factory, execution, orders)
    holder["reconciliation"] = reconciliation
    notifications = NotificationService(factory, max_attempts=4)
    positions = PositionManager(factory, orders, after_commit=notifications.attempt)
    return TradingEngine(
        session_factory=factory,
        settings=settings,
        user_id=user_id,
        orders=orders,
        positions=positions,
        reconciliation=reconciliation,
        watchdog=Watchdog(factory),
    )


async def test_start_and_menu_read_state(settings) -> None:
    state = FakeState()
    message = FakeMessage("/start", 15)
    await process_start(message, settings, state)
    assert state.state is Onboarding.mode
    assert message.answers
    launch = FakeMessage("🚀 ЗАПУСТИТЬ", 15)
    data = FakeState()
    data.data = {"mode": "paper", "amount": "1500", "risk": "low", "symbols": "BTC-USDT, TON-USDT"}

    async def listed() -> set[str]:
        return {"BTC-USDT", "ETH-USDT"}

    await on_launch(launch, settings, data, listed)
    assert await has_bot(15)
    assert any("TON-USDT" in line and "отброшена" in line for line in launch.answers)
    assert any("Запуск записан" in line for line in launch.answers)
    home = FakeMessage("🤖 Автоторговля", 15)
    await process_menu(home, settings)
    screen = home.answers[-1]
    assert "paper" in screen
    assert "1500" in screen
    assert "Биржевые ордера выключены" in screen


async def test_launch_refuses_when_the_symbol_list_is_down(settings) -> None:
    state = FakeState()
    state.data = {"mode": "paper", "amount": "1000", "risk": "medium", "symbols": ""}

    async def down() -> set[str]:
        raise RuntimeError("exchange down")

    message = FakeMessage("🚀 ЗАПУСТИТЬ", 16)
    await on_launch(message, settings, state, down)
    assert not await has_bot(16)
    assert any("Запуск отменён" in line for line in message.answers)


async def test_token_is_encrypted_and_the_message_is_deleted(settings) -> None:
    secret = "super-secret-token"
    message = FakeMessage(secret, 21)
    state = FakeState()
    await on_token(message, settings, state)
    assert message.deleted is True
    assert secret not in "\n".join(message.answers)
    assert state.state is Onboarding.launch
    factory = get_sessionmaker()
    async with factory() as session:
        account = await session.scalar(select(XRocketAccount))
    assert account is not None and account.encrypted_api_token is not None
    assert secret not in account.encrypted_api_token
    assert decrypt_secret(account.encrypted_api_token, settings.encryption_key.get_secret_value()) == secret


async def test_mainnet_stays_unchanged_without_the_exact_phrase(settings) -> None:
    factory = get_sessionmaker()
    async with factory() as session:
        user = User(telegram_id=30, username="daniel")
        session.add(user)
        await session.flush()
        session.add(
            BotSettings(
                user_id=user.id,
                execution_mode="paper",
                risk_profile="medium",
                enabled_symbols="BTC-USDT",
                live_confirmed=False,
            )
        )
        await session.commit()
    state = FakeState()
    query = FakeCallback(30)
    await on_mainnet(query, state)
    assert state.state is MainnetGate.phrase
    prompt = await prepare_mainnet(30)
    assert "START LIVE" in prompt
    await _assert_mode(factory, "paper", False)

    async def listed() -> set[str]:
        return {"TON-USDT", "BTC-USDT"}

    lower = FakeMessage("start live", 30)
    await on_mainnet_phrase(lower, settings, FakeState(), listed)
    assert any("остаётся прежним" in line for line in lower.answers)
    await _assert_mode(factory, "paper", False)

    live_settings = settings.model_copy(update={"xrocket_env": "mainnet"})
    missing = FakeMessage("START LIVE", 30)

    async def no_ton() -> set[str]:
        return {"BTC-USDT"}

    await on_mainnet_phrase(missing, live_settings, FakeState(), no_ton)
    assert any("TON-USDT" in line for line in missing.answers)
    await _assert_mode(factory, "paper", False)

    testnet_phrase = FakeMessage("START LIVE", 30)
    await on_mainnet_phrase(testnet_phrase, settings, FakeState(), listed)
    assert any("mainnet" in line for line in testnet_phrase.answers)
    await _assert_mode(factory, "paper", False)

    ready = FakeMessage("START LIVE", 30)
    done = FakeState()
    await on_mainnet_phrase(ready, live_settings, done, listed)
    assert any("MAINNET записан" in line for line in ready.answers)
    assert done.state is None
    await _assert_mode(factory, "mainnet", True)


async def test_admin_is_hidden_from_other_users(settings) -> None:
    admin_settings = settings.model_copy(
        update={"admin_ids": "7", "bot_token": SecretStr("123456:telegram-bot-secret")}
    )
    factory = get_sessionmaker()
    async with factory() as session:
        user = User(telegram_id=7, username="admin")
        session.add(user)
        await session.flush()
        session.add(
            BotSettings(
                user_id=user.id,
                execution_mode="paper",
                risk_profile="low",
                enabled_symbols="BTC-USDT",
            )
        )
        session.add(
            XRocketAccount(
                user_id=user.id,
                encrypted_api_token="ciphertext-should-stay-hidden",
                environment="testnet",
                is_connected=True,
            )
        )
        session.add(Heartbeat(service="trading", status="ok", details="hidden-heartbeat-detail", last_seen_at=datetime.now(timezone.utc)))
        await session.commit()
    stranger = FakeMessage("/admin", 99)
    await on_admin(stranger, admin_settings)
    refusal = stranger.answers[-1]
    assert "только администраторам" in refusal
    assert "ciphertext-should-stay-hidden" not in refusal
    assert "hidden-heartbeat-detail" not in refusal
    assert admin_settings.encryption_key.get_secret_value() not in refusal

    admin = FakeMessage("/admin", 7)
    await process_admin(admin, admin_settings)
    report = admin.answers[-1]
    assert "heartbeat trading: ok" in report
    assert "ciphertext-should-stay-hidden" not in report
    assert "hidden-heartbeat-detail" not in report
    assert admin_settings.encryption_key.get_secret_value() not in report
    assert admin_settings.bot_token.get_secret_value() not in report


async def test_pause_and_stop_only_set_flags(settings) -> None:
    factory = get_sessionmaker()
    async with factory() as session:
        user = User(telegram_id=40, username="daniel")
        session.add(user)
        await session.flush()
        session.add(
            BotSettings(
                user_id=user.id,
                execution_mode="testnet",
                risk_profile="medium",
                enabled_symbols="ETH-USDT",
                paper_cash=Decimal("800"),
                paper_seed=Decimal("800"),
            )
        )
        await session.commit()
        user_id = user.id
    paused = await toggle_pause(40, settings)
    assert "Пауза включена" in paused
    async with factory() as session:
        bot = await session.scalar(select(BotSettings).where(BotSettings.user_id == user_id))
        note = await session.scalar(select(Notification).where(Notification.user_id == user_id))
    assert bot is not None and bot.is_paused is True
    assert note is not None and note.event_type == "paused"
    query = FakeCallback(40)
    await process_stop(query, settings, close_positions=False)
    async with factory() as session:
        bot = await session.scalar(select(BotSettings).where(BotSettings.user_id == user_id))
    assert bot is not None and bot.emergency_stop is True and bot.close_positions_requested is False
    blocked = await toggle_pause(40, settings)
    assert "Сначала снимите" in blocked
    close = FakeCallback(40)
    await process_stop(close, settings, close_positions=True)
    async with factory() as session:
        bot = await session.scalar(select(BotSettings).where(BotSettings.user_id == user_id))
        orders = list((await session.scalars(select(Order))).all())
    assert bot is not None and bot.close_positions_requested is True
    assert orders == []


async def test_close_request_is_executed_by_the_engine(settings) -> None:
    factory = get_sessionmaker()
    user_id = await make_account(factory, emergency_stop=True)
    async with factory() as session:
        bot = await session.scalar(select(BotSettings).where(BotSettings.user_id == user_id))
        assert bot is not None
        bot.close_positions_requested = True
        await session.commit()
    await add_position(
        factory,
        user_id=user_id,
        symbol="TON-USDT",
        entry=Decimal("12"),
        quantity=Decimal("1"),
        stop=Decimal("10"),
        take_profit=Decimal("20"),
    )
    execution = PaperExecution(fee_rate=Decimal("0.01"), slippage_fraction=Decimal("0.001"))
    engine = _engine(factory, execution, user_id, settings)
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    engine.push_snapshot(
        MarketSnapshot(
            symbol="TON-USDT",
            bars=[],
            book=book_around(Decimal("12")),
            rules=make_symbol(),
            api_ok=True,
            stale=False,
            fetched_at=now,
        )
    )
    await engine.run_cycle(now=now)
    assert execution.buy_calls == 0
    assert execution.sell_calls == 1
    async with factory() as session:
        bot = await session.scalar(select(BotSettings).where(BotSettings.user_id == user_id))
        position = await session.scalar(select(Position))
    assert bot is not None and bot.close_positions_requested is False
    assert position is not None and position.status == "closed"


async def _assert_mode(factory, mode: str, live: bool) -> None:
    async with factory() as session:
        bot = await session.scalar(select(BotSettings))
    assert bot is not None
    assert bot.execution_mode == mode
    assert bot.live_confirmed is live
