"""Short setup flows. Trading decisions stay in the core."""

from aiogram.fsm.state import State, StatesGroup


class Onboarding(StatesGroup):
    mode = State()
    amount = State()
    risk = State()
    pairs = State()
    token = State()
    launch = State()


class MainnetGate(StatesGroup):
    phrase = State()
