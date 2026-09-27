"""Thin Aiogram handlers. Each one calls bot.commands and returns."""

from collections.abc import Awaitable, Callable
from decimal import Decimal

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.commands import (
    apply_mainnet_phrase,
    complete_onboarding,
    prepare_mainnet,
    process_admin,
    process_clear_stop,
    process_menu,
    process_start,
    process_stop,
    process_token,
)
from bot.keyboards import (
    MENU_TEXTS,
    RISK_BUTTONS,
    launch_keyboard,
    main_menu,
    pairs_keyboard,
    risk_keyboard,
    token_keyboard,
)
from bot.states import MainnetGate, Onboarding
from core.config import Settings

router = Router(name="autotrade")

SymbolLister = Callable[[], Awaitable[set[str]]]


@router.message(CommandStart())
async def on_start(message: Message, settings: Settings, state: FSMContext) -> None:
    await process_start(message, settings, state)


@router.message(Command("admin"))
async def on_admin(message: Message, settings: Settings) -> None:
    await process_admin(message, settings)


@router.message(F.text.in_(set(MENU_TEXTS)))
async def on_menu(message: Message, settings: Settings, state: FSMContext) -> None:
    await state.clear()
    await process_menu(message, settings)


@router.callback_query(F.data == "stop:close")
async def on_stop_close(query: CallbackQuery, settings: Settings) -> None:
    await process_stop(query, settings, close_positions=True)


@router.callback_query(F.data == "stop:leave")
async def on_stop_leave(query: CallbackQuery, settings: Settings) -> None:
    await process_stop(query, settings, close_positions=False)


@router.callback_query(F.data == "settings:clear-stop")
async def on_clear_stop(query: CallbackQuery) -> None:
    await process_clear_stop(query)


@router.callback_query(F.data == "settings:mainnet")
async def on_mainnet(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(MainnetGate.phrase)
    await query.answer()
    if query.message is not None:
        await query.message.answer(await prepare_mainnet(_query_user(query)))


@router.message(Onboarding.mode, F.text.in_({"PAPER", "TESTNET"}))
async def on_mode(message: Message, state: FSMContext) -> None:
    mode = "paper" if message.text == "PAPER" else "testnet"
    await state.update_data(mode=mode)
    await state.set_state(Onboarding.amount)
    await message.answer("Введите стартовый бумажный капитал в USDT. Например: 1000")


@router.message(Onboarding.mode)
async def on_mode_again(message: Message) -> None:
    await message.answer("Выберите PAPER или TESTNET кнопкой.")


@router.message(Onboarding.amount)
async def on_amount(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").replace(",", ".").strip()
    try:
        amount = Decimal(raw)
    except Exception:
        await message.answer("Нужно число больше нуля. Например: 1000")
        return
    if amount <= 0:
        await message.answer("Нужно число больше нуля. Например: 1000")
        return
    await state.update_data(amount=str(amount))
    await state.set_state(Onboarding.risk)
    await message.answer("Выберите риск на сделку.", reply_markup=risk_keyboard())


@router.message(Onboarding.risk, F.text.in_(set(RISK_BUTTONS)))
async def on_risk(message: Message, state: FSMContext) -> None:
    label = message.text or ""
    await state.update_data(risk=RISK_BUTTONS[label])
    await state.set_state(Onboarding.pairs)
    await message.answer(
        "Нажмите «По умолчанию» или пришлите пары через запятую, например BTC-USDT, ETH-USDT.",
        reply_markup=pairs_keyboard(),
    )


@router.message(Onboarding.risk)
async def on_risk_again(message: Message) -> None:
    await message.answer("Выберите риск кнопкой: Низкий, Средний или Высокий.")


@router.message(Onboarding.pairs)
async def on_pairs(message: Message, state: FSMContext) -> None:
    raw = message.text or ""
    if raw == "По умолчанию":
        raw = ""
    await state.update_data(symbols=raw)
    await state.set_state(Onboarding.token)
    await message.answer(
        "Пришлите API-токен xRocket одним сообщением. Оно будет удалено, в базе останется только шифр. "
        "Для paper токен не нужен — нажмите «Пропустить».",
        reply_markup=token_keyboard(),
    )


@router.message(Onboarding.token, F.text == "Пропустить")
async def on_skip_token(message: Message, state: FSMContext) -> None:
    await state.set_state(Onboarding.launch)
    await message.answer("Токен пропущен. Нажмите «🚀 ЗАПУСТИТЬ».", reply_markup=launch_keyboard())


@router.message(Onboarding.token)
async def on_token(message: Message, settings: Settings, state: FSMContext) -> None:
    await process_token(message, settings)
    await state.set_state(Onboarding.launch)
    await message.answer("Нажмите «🚀 ЗАПУСТИТЬ».", reply_markup=launch_keyboard())


@router.message(Onboarding.launch, F.text == "🚀 ЗАПУСТИТЬ")
async def on_launch(message: Message, settings: Settings, state: FSMContext, list_symbols: SymbolLister) -> None:
    data = await state.get_data()
    try:
        listed = await list_symbols()
    except Exception:
        await message.answer("Не удалось получить список пар с биржи. Запуск отменён.")
        return
    amount_raw = str(data.get("amount", "0"))
    try:
        amount = Decimal(amount_raw)
    except Exception:
        await message.answer("Стартовая сумма потерялась. Начните заново командой /start.")
        return
    ok, text = await complete_onboarding(
        _message_user(message),
        mode=str(data.get("mode", "")),
        amount=amount,
        risk=str(data.get("risk", "")),
        symbols_raw=str(data.get("symbols", "")),
        listed=listed,
        settings=settings,
    )
    await message.answer(text, reply_markup=main_menu() if ok else None)
    if ok:
        await state.clear()


@router.message(MainnetGate.phrase)
async def on_mainnet_phrase(
    message: Message,
    settings: Settings,
    state: FSMContext,
    list_symbols: SymbolLister,
) -> None:
    try:
        listed = await list_symbols()
    except Exception:
        await message.answer("Не удалось получить список пар с биржи. Режим остаётся прежним.")
        return
    ok, text = await apply_mainnet_phrase(
        _message_user(message),
        message.text or "",
        settings=settings,
        listed=listed,
    )
    await message.answer(text, reply_markup=main_menu())
    if ok:
        await state.clear()


def _message_user(message: Message) -> int:
    if message.from_user is None:
        return 0
    return message.from_user.id


def _query_user(query: CallbackQuery) -> int:
    if query.from_user is None:
        return 0
    return query.from_user.id
