"""Historical candles from public REST, cached in JSON and SQLite."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from trading.market import Bar, api_candle_interval, timeframe_seconds
from xrocket.models import Candle
from xrocket.rest_client import XRocketRestClient

# bars_from_candles lives in services and pulls the REST client types.
# This module converts Candle rows itself so a cache round-trip stays local.


def bars_from_candles(candles: list[Candle]) -> list[Bar]:
    return [
        Bar(
            open_time=_aware(candle.start),
            open=candle.open,
            high=candle.high,
            low=candle.low,
            close=candle.close,
            volume=candle.base_volume,
        )
        for candle in candles
    ]


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


class CandleCache:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._db_path = root / "candles.db"
        self._ensure_db()

    def load(self, symbol: str, timeframe: str) -> list[Bar]:
        with sqlite3.connect(self._db_path) as connection:
            rows = connection.execute(
                """
                SELECT open_time, open, high, low, close, volume
                FROM candles
                WHERE symbol = ? AND timeframe = ?
                ORDER BY open_time
                """,
                (symbol, timeframe),
            ).fetchall()
        bars = [
            Bar(
                open_time=_aware(datetime.fromisoformat(row[0])),
                open=Decimal(row[1]),
                high=Decimal(row[2]),
                low=Decimal(row[3]),
                close=Decimal(row[4]),
                volume=Decimal(row[5]),
            )
            for row in rows
        ]
        if bars:
            return bars
        path = self._json_path(symbol, timeframe)
        if not path.is_file():
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
        return [
            Bar(
                open_time=_aware(datetime.fromisoformat(item["open_time"])),
                open=Decimal(item["open"]),
                high=Decimal(item["high"]),
                low=Decimal(item["low"]),
                close=Decimal(item["close"]),
                volume=Decimal(item["volume"]),
            )
            for item in payload
        ]

    def save(self, symbol: str, timeframe: str, bars: list[Bar]) -> None:
        merged = _merge(self.load(symbol, timeframe), bars)
        with sqlite3.connect(self._db_path) as connection:
            connection.executemany(
                """
                INSERT INTO candles (symbol, timeframe, open_time, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, timeframe, open_time) DO UPDATE SET
                    open = excluded.open,
                    high = excluded.high,
                    low = excluded.low,
                    close = excluded.close,
                    volume = excluded.volume
                """,
                [
                    (
                        symbol,
                        timeframe,
                        bar.open_time.isoformat(),
                        format(bar.open, "f"),
                        format(bar.high, "f"),
                        format(bar.low, "f"),
                        format(bar.close, "f"),
                        format(bar.volume, "f"),
                    )
                    for bar in merged
                ],
            )
        path = self._json_path(symbol, timeframe)
        path.write_text(
            json.dumps(
                [
                    {
                        "open_time": bar.open_time.isoformat(),
                        "open": format(bar.open, "f"),
                        "high": format(bar.high, "f"),
                        "low": format(bar.low, "f"),
                        "close": format(bar.close, "f"),
                        "volume": format(bar.volume, "f"),
                    }
                    for bar in merged
                ]
            ),
            encoding="utf-8",
        )

    def _json_path(self, symbol: str, timeframe: str) -> Path:
        return self.root / f"{symbol}_{timeframe}.json"

    def _ensure_db(self) -> None:
        with sqlite3.connect(self._db_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS candles (
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    open_time TEXT NOT NULL,
                    open TEXT NOT NULL,
                    high TEXT NOT NULL,
                    low TEXT NOT NULL,
                    close TEXT NOT NULL,
                    volume TEXT NOT NULL,
                    PRIMARY KEY (symbol, timeframe, open_time)
                )
                """
            )


def covers(bars: list[Bar], start: datetime, end: datetime, timeframe: str) -> bool:
    if not bars:
        return False
    oldest = min(bar.open_time for bar in bars)
    newest = max(bar.open_time for bar in bars)
    slack = timedelta(seconds=timeframe_seconds(timeframe) * 2)
    return oldest <= start and newest >= end - slack


def slice_bars(bars: list[Bar], start: datetime) -> list[Bar]:
    return [bar for bar in bars if bar.open_time >= start]


async def load_history(
    symbol: str,
    days: int,
    *,
    rest_url: str,
    cache_dir: Path,
    timeframe: str = "15m",
) -> list[Bar]:
    """Return closed-enough candles. A fresh cache is used before another REST call."""
    if days < 1:
        raise ValueError("days must be >= 1")
    interval = api_candle_interval(timeframe)
    cache = CandleCache(cache_dir)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    cached = cache.load(symbol, interval)
    if covers(cached, start, end, timeframe):
        return slice_bars(cached, start)
    client = XRocketRestClient(rest_url, token=None)
    try:
        candles = await client.get_candles(symbol, interval, start, end)
    finally:
        await client.close()
    fresh = bars_from_candles(candles)
    cache.save(symbol, interval, fresh)
    return slice_bars(cache.load(symbol, interval), start)


def _merge(left: list[Bar], right: list[Bar]) -> list[Bar]:
    by_time = {bar.open_time: bar for bar in left}
    for bar in right:
        by_time[bar.open_time] = bar
    return [by_time[key] for key in sorted(by_time)]
