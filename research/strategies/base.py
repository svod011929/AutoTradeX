"""Closed-bar actions a research strategy can request.

The engine fills on a later bar. ``action`` and ``on_position`` must only use
data up to the index they are given.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Never, Protocol

from trading.market import Bar

TimeframeName = Literal["1h", "4h", "1d"]
RiskMode = Literal["stake", "risk"]


def _assert_never(value: Never) -> Never:
    raise AssertionError(f"unhandled timeframe: {value}")


def bars_per_day(timeframe: TimeframeName) -> int:
    match timeframe:
        case "1h":
            return 24
        case "4h":
            return 6
        case "1d":
            return 1
        case _ as unreachable:
            _assert_never(unreachable)


@dataclass(frozen=True)
class Action:
    enter: bool = False
    exit: bool = False
    stop: Decimal | None = None
    resting_stop: bool = False
    limit: Decimal | None = None
    timeout: int = 0
    risk_mode: RiskMode = "risk"


class EntryStrategy(Protocol):
    def sync(self, bars: list[Bar]) -> None:
        """Prepare indicators for this closed series."""

    def action(self, index: int) -> Action:
        """Signal at the close of ``bars[index]``."""

    def on_position(self, index: int, held_high: Decimal) -> Action:
        """Update a position after ``bars[index]`` has closed without a stop fill."""
