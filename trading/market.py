"""Bars, order-book snapshots, and the candle-close rule.

A bar is closed when its timeframe has ended and the grace has passed.
The caller still has to see that bar in a REST candle response. The next
WebSocket print is not required, and a bar that is still open is not used.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

TIMEFRAME_SECONDS: dict[str, int] = {
    "1m": 60,
    "1min": 60,
    "5m": 300,
    "5min": 300,
    "15m": 900,
    "15min": 900,
    "30m": 1800,
    "30min": 1800,
    "1h": 3600,
    "1hour": 3600,
    "4h": 14400,
    "4hour": 14400,
    "1d": 86400,
    "1day": 86400,
}

API_INTERVAL: dict[str, str] = {
    "15m": "15min",
    "15min": "15min",
    "1m": "1min",
    "1min": "1min",
    "5m": "5min",
    "5min": "5min",
    "30m": "30min",
    "30min": "30min",
    "1h": "1hour",
    "1hour": "1hour",
    "4h": "4hour",
    "4hour": "4hour",
    "1d": "1day",
    "1day": "1day",
}


def ensure_aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def timeframe_seconds(timeframe: str) -> int:
    try:
        return TIMEFRAME_SECONDS[timeframe]
    except KeyError as exc:
        raise ValueError(f"unsupported timeframe: {timeframe}") from exc


def api_candle_interval(timeframe: str) -> str:
    try:
        return API_INTERVAL[timeframe]
    except KeyError as exc:
        raise ValueError(f"unsupported timeframe: {timeframe}") from exc


def candle_is_closed(open_time: datetime, timeframe: str, now: datetime, grace_seconds: int) -> bool:
    """True once start + timeframe + grace has been reached."""
    opened = ensure_aware(open_time)
    current = ensure_aware(now)
    deadline = opened + timedelta(seconds=timeframe_seconds(timeframe) + grace_seconds)
    return current >= deadline


@dataclass(frozen=True)
class Bar:
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True)
class BookLevel:
    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class BookSnapshot:
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    is_full_snapshot: bool

    @property
    def best_bid(self) -> BookLevel | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> BookLevel | None:
        return self.asks[0] if self.asks else None


def sort_book(bids: list[BookLevel], asks: list[BookLevel]) -> BookSnapshot:
    ordered_bids = tuple(sorted(bids, key=lambda level: level.price, reverse=True))
    ordered_asks = tuple(sorted(asks, key=lambda level: level.price))
    return BookSnapshot(bids=ordered_bids, asks=ordered_asks, is_full_snapshot=True)


def walk_book(levels: tuple[BookLevel, ...] | list[BookLevel], quantity: Decimal) -> Decimal | None:
    """Volume-weighted price to buy or sell ``quantity``. None when the book is too thin."""
    if quantity <= 0:
        return None
    left = quantity
    notional = Decimal("0")
    for level in levels:
        if level.size <= 0 or level.price <= 0:
            continue
        take = level.size if level.size < left else left
        notional += take * level.price
        left -= take
        if left <= 0:
            return notional / quantity
    return None


def confirmed_closed_bars(
    bars: list[Bar],
    *,
    timeframe: str,
    now: datetime,
    grace_seconds: int,
) -> list[Bar]:
    """Keep REST bars whose own window has ended. Drop the still-open bar."""
    closed = [bar for bar in bars if candle_is_closed(bar.open_time, timeframe, now, grace_seconds)]
    return sorted(closed, key=lambda bar: bar.open_time)
