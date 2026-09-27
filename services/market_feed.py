"""Public REST candles and the full order book.

No token is required. A failed fetch is marked stale so the engine does not trade.
The forming candle is left in the list; the engine drops it by the close rule.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from trading.market import Bar, BookLevel, BookSnapshot, api_candle_interval, ensure_aware, sort_book, timeframe_seconds
from xrocket.models import Candle, OrderBook, Symbol
from xrocket.rest_client import XRocketRestClient

logger = logging.getLogger("autotrade.market")


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    bars: list[Bar]
    book: BookSnapshot
    rules: Symbol | None
    api_ok: bool
    stale: bool
    fetched_at: datetime


class MarketSource(Protocol):
    async def load(self, symbol: str, timeframe: str, now: datetime) -> MarketSnapshot:
        """Return the latest public snapshot for one symbol."""


def empty_snapshot(symbol: str, now: datetime) -> MarketSnapshot:
    return MarketSnapshot(
        symbol=symbol,
        bars=[],
        book=BookSnapshot(bids=(), asks=(), is_full_snapshot=False),
        rules=None,
        api_ok=False,
        stale=True,
        fetched_at=now,
    )


def bars_from_candles(candles: list[Candle]) -> list[Bar]:
    return [
        Bar(
            open_time=ensure_aware(candle.start),
            open=candle.open,
            high=candle.high,
            low=candle.low,
            close=candle.close,
            volume=candle.base_volume,
        )
        for candle in candles
    ]


def book_from_rest(book: OrderBook) -> BookSnapshot:
    bids = [BookLevel(price=level.price, size=level.size) for level in book.bids]
    asks = [BookLevel(price=level.price, size=level.size) for level in book.asks]
    return sort_book(bids, asks)


class PublicRestFeed:
    """Polls public testnet REST. This does not place orders and does not use a token."""

    def __init__(self, client: XRocketRestClient, *, depth: int, history_days: int = 5) -> None:
        self._client = client
        self._depth = depth
        self._history = timedelta(days=history_days)
        self._rules: dict[str, Symbol] = {}

    async def load(self, symbol: str, timeframe: str, now: datetime) -> MarketSnapshot:
        moment = ensure_aware(now)
        try:
            candles = await self._client.get_candles(
                symbol,
                api_candle_interval(timeframe),
                moment - self._history,
                moment,
            )
            raw_book = await self._client.get_orderbook(symbol, depth=self._depth)
            rules = await self._symbol(symbol)
        except Exception as exc:
            logger.warning("public market data failed for %s: %s", symbol, exc)
            return empty_snapshot(symbol, moment)
        bars = bars_from_candles(candles)
        book = book_from_rest(raw_book)
        stale = _is_stale(bars, book, moment, timeframe)
        return MarketSnapshot(
            symbol=symbol,
            bars=bars,
            book=book,
            rules=rules,
            api_ok=True,
            stale=stale,
            fetched_at=moment,
        )

    async def _symbol(self, symbol: str) -> Symbol:
        cached = self._rules.get(symbol)
        if cached is not None:
            return cached
        loaded = await self._client.get_symbol(symbol)
        self._rules[symbol] = loaded
        return loaded


def _is_stale(bars: list[Bar], book: BookSnapshot, now: datetime, timeframe: str) -> bool:
    if not book.is_full_snapshot or book.best_bid is None or book.best_ask is None:
        return True
    if not bars:
        return True
    newest = max(bar.open_time for bar in bars)
    age = ensure_aware(now) - ensure_aware(newest)
    return age > timedelta(seconds=timeframe_seconds(timeframe) * 2)
