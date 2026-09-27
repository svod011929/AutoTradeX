"""Reply and inline keyboards for the Telegram menu."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

MENU_TEXTS: tuple[str, ...] = (
    "🤖 Автоторговля",
    "💰 Баланс",
    "📊 Портфель",
    "📈 Статистика",
    "📜 История",
    "🔔 Уведомления",
    "⚙️ Настройки",
    "⏸ Пауза",
    "🛑 STOP",
)

RISK_BUTTONS: dict[str, str] = {
    "Низкий": "low",
    "Средний": "medium",
    "Высокий": "high",
}


def main_menu() -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=MENU_TEXTS[0]), KeyboardButton(text=MENU_TEXTS[1])],
        [KeyboardButton(text=MENU_TEXTS[2]), KeyboardButton(text=MENU_TEXTS[3])],
        [KeyboardButton(text=MENU_TEXTS[4]), KeyboardButton(text=MENU_TEXTS[5])],
        [KeyboardButton(text=MENU_TEXTS[6]), KeyboardButton(text=MENU_TEXTS[7])],
        [KeyboardButton(text=MENU_TEXTS[8])],
    ]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def mode_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="PAPER"), KeyboardButton(text="TESTNET")]],
        resize_keyboard=True,
    )


def risk_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=label) for label in RISK_BUTTONS]],
        resize_keyboard=True,
    )


def pairs_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="По умолчанию")]],
        resize_keyboard=True,
    )


def token_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Пропустить")]],
        resize_keyboard=True,
    )


def launch_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🚀 ЗАПУСТИТЬ")]],
        resize_keyboard=True,
    )


def stop_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Закрыть позиции", callback_data="stop:close")],
            [InlineKeyboardButton(text="Оставить позиции", callback_data="stop:leave")],
        ]
    )


def settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Снять аварийную остановку", callback_data="settings:clear-stop")],
            [InlineKeyboardButton(text="Перейти в MAINNET", callback_data="settings:mainnet")],
        ]
    )
