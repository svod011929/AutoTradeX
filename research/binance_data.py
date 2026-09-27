"""Public Binance spot klines from data.binance.vision, cached as CSV.

No API key. Monthly files are the long history; the current month is daily
files. Timestamps are milliseconds in older files and microseconds in newer
ones. The cache is not committed.
"""

import csv
import io
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from trading.market import Bar, timeframe_seconds

from research.strategies.base import TimeframeName

VISION_ROOT = "https://data.binance.vision/data/spot"
CACHE_DIR = Path("data/research/binance")
HISTORY_START = datetime(2018, 1, 1, tzinfo=timezone.utc)


def cache_path(symbol: str, timeframe: TimeframeName) -> Path:
    return CACHE_DIR / f"{symbol}_{timeframe}.csv"


def load_cached(symbol: str, timeframe: TimeframeName) -> list[Bar]:
    path = cache_path(symbol, timeframe)
    if not path.exists() or path.stat().st_size == 0:
        raise FileNotFoundError(path)
    bars: list[Bar] = []
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            opened = datetime.fromtimestamp(int(row["open_time_ms"]) / 1000, tz=timezone.utc)
            bars.append(
                Bar(
                    open_time=opened,
                    open=Decimal(row["open"]),
                    high=Decimal(row["high"]),
                    low=Decimal(row["low"]),
                    close=Decimal(row["close"]),
                    volume=Decimal(row["volume"]),
                )
            )
    return _drop_open_candle(bars, timeframe)


def download(symbol: str, timeframe: TimeframeName, *, pause: float = 0.05) -> list[Bar]:
    """Fetch closed candles from 2018-01-01 into the CSV cache."""
    path = cache_path(symbol, timeframe)
    path.parent.mkdir(parents=True, exist_ok=True)
    if _cache_is_fresh(path):
        return load_cached(symbol, timeframe)
    rows = _fetch_all(symbol, timeframe, pause)
    if not rows:
        raise RuntimeError(f"no Binance candles for {symbol} {timeframe}")
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write("open_time_ms,open,high,low,close,volume\n")
        for opened, open_, high, low, close, volume in rows:
            handle.write(f"{opened},{open_},{high},{low},{close},{volume}\n")
    return load_cached(symbol, timeframe)


def open_time_to_ms(raw: int) -> int:
    """Vision files use milliseconds or, from 2025, microseconds."""
    if raw >= 10**15:
        return raw // 1000
    return raw


def _cache_is_fresh(path: Path) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    with path.open(encoding="utf-8") as handle:
        header = handle.readline()
        first = handle.readline().strip()
        if not header or not first:
            return False
        rest = handle.readlines()
    last = rest[-1].strip() if rest else first
    first_ms = int(first.split(",")[0])
    last_ms = int(last.split(",")[0])
    if datetime.fromtimestamp(first_ms / 1000, tz=timezone.utc) > HISTORY_START + timedelta(days=10):
        return False
    age = datetime.now(timezone.utc) - datetime.fromtimestamp(last_ms / 1000, tz=timezone.utc)
    return age <= timedelta(days=3) and (1 + len(rest)) > 1000


def _fetch_all(symbol: str, timeframe: str, pause: float) -> list[tuple[int, str, str, str, str, str]]:
    now = datetime.now(timezone.utc)
    collected: dict[int, tuple[int, str, str, str, str, str]] = {}
    month = HISTORY_START
    last_full = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    while month < last_full:
        stamp = f"{month.year:04d}-{month.month:02d}"
        added = _read_zip(symbol, timeframe, "monthly", stamp, pause)
        for row in added:
            collected[row[0]] = row
        if month.month == 12:
            month = datetime(month.year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            month = datetime(month.year, month.month + 1, 1, tzinfo=timezone.utc)
    day = last_full
    yesterday = (now - timedelta(days=1)).date()
    while day.date() <= yesterday:
        stamp = day.date().isoformat()
        added = _read_zip(symbol, timeframe, "daily", stamp, pause)
        if not added and day.date() < yesterday:
            day += timedelta(days=1)
            continue
        for row in added:
            collected[row[0]] = row
        day += timedelta(days=1)
    return [collected[key] for key in sorted(collected)]


def _read_zip(
    symbol: str,
    timeframe: str,
    kind: str,
    stamp: str,
    pause: float,
) -> list[tuple[int, str, str, str, str, str]]:
    name = f"{symbol}-{timeframe}-{stamp}.zip"
    url = f"{VISION_ROOT}/{kind}/klines/{symbol}/{timeframe}/{name}"
    blob = _get(url, pause)
    if blob is None:
        return []
    archive = zipfile.ZipFile(io.BytesIO(blob))
    csv_name = next(item for item in archive.namelist() if item.endswith(".csv"))
    rows: list[tuple[int, str, str, str, str, str]] = []
    for line in archive.read(csv_name).decode().splitlines():
        parts = line.split(",")
        if len(parts) < 6 or not parts[0].isdigit():
            continue
        opened = open_time_to_ms(int(parts[0]))
        if opened < int(HISTORY_START.timestamp() * 1000):
            continue
        rows.append((opened, parts[1], parts[2], parts[3], parts[4], parts[5]))
    print(f"vision {symbol} {timeframe} {stamp} {len(rows)}", flush=True)
    return rows


def _get(url: str, pause: float) -> bytes | None:
    request = urllib.request.Request(url, headers={"User-Agent": "autotrade-research"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = response.read()
            if pause:
                time.sleep(pause)
            return payload
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            if attempt == 3:
                raise
        except Exception:
            if attempt == 3:
                raise
        time.sleep(1.5 * (attempt + 1))
    return None


def _drop_open_candle(bars: list[Bar], timeframe: TimeframeName) -> list[Bar]:
    if not bars:
        return []
    step = timedelta(seconds=timeframe_seconds(timeframe))
    now = datetime.now(timezone.utc)
    return [bar for bar in bars if bar.open_time + step <= now]
