"""xRocket WebSocket client.

Market data stays in memory. Nothing from the socket is written to SQLite.
Closed candles are the only stream that invokes a callback. A candle is treated
as closed when a later open time arrives, because a closed flag is NOT DOCUMENTED.
"""

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import aiohttp

from core.config import Settings
from xrocket.authentication import ws_auth_message
from xrocket.exceptions import InvalidTokenError, UserBlockedError
from xrocket.models import AssetBalance, Candle, ExchangeOrder, OrderBook, PublicTrade, Ticker

TICKER_INTERVALS = frozenset(
    {
        "1min",
        "5min",
        "15min",
        "30min",
        "1hour",
        "2hour",
        "4hour",
        "8hour",
        "12hour",
        "1day",
    }
)
CANDLE_INTERVALS = TICKER_INTERVALS | {"1week", "1month"}
ORDERBOOK_DEPTHS = frozenset({5, 10, 20, 50, 100, 200, 500})
_FATAL_WS_CODES = frozenset({-32030, -32034})

ClosedCandleCallback = Callable[[Candle, str, str], Awaitable[None] | None]
Jitter = Callable[[], float]


class WebSocketLike(Protocol):
    async def send_json(self, data: dict[str, object]) -> None: ...

    async def receive_json(self) -> dict[str, object]: ...

    async def close(self) -> None: ...


class _StopClient(Exception):
    """Auth was rejected. Reconnect would hammer the exchange with a bad token."""


@dataclass(frozen=True)
class _Subscription:
    channel: str
    params: dict[str, object]
    stream_key: str


class XRocketWebSocketClient:
    def __init__(
        self,
        url: str,
        token: str | None = None,
        *,
        ping_interval: float = 30,
        stale_seconds: float = 90,
        reconnect_base_seconds: float = 1,
        reconnect_max_seconds: float = 30,
        opener: Callable[[], Awaitable[WebSocketLike]] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        clock: Callable[[], float] | None = None,
        jitter: Jitter | None = None,
        on_closed_candle: ClosedCandleCallback | None = None,
    ) -> None:
        self._url = url
        self._token = token.strip() if token else None
        self._ping_interval = ping_interval
        self._stale_seconds = stale_seconds
        self._reconnect_base = reconnect_base_seconds
        self._reconnect_max = reconnect_max_seconds
        self._opener = opener or self._open_default
        self._sleep = sleep or asyncio.sleep
        self._clock = clock or time.monotonic
        self._jitter = jitter or _default_jitter
        self._on_closed_candle = on_closed_candle
        self._log = logging.getLogger("autotrade.xrocket.ws")
        self._running = False
        self._connected = False
        self._socket: WebSocketLike | None = None
        self._http: aiohttp.ClientSession | None = None
        self._owns_http = False
        self._seq = 0
        self._subs: dict[str, _Subscription] = {}
        self._pending: dict[str, asyncio.Future[dict[str, object]]] = {}
        self._seen_at: dict[str, float] = {}
        self._tickers: dict[str, Ticker] = {}
        self._books: dict[str, OrderBook] = {}
        self._book_reliable: dict[str, bool] = {}
        self._book_increment_pending: dict[str, bool] = {}
        self._closed: dict[tuple[str, str], Candle] = {}
        self._forming: dict[tuple[str, str], Candle] = {}
        self._last_trade: dict[str, PublicTrade] = {}
        self._ws_balances: list[AssetBalance] = []
        self._active_orders: list[ExchangeOrder] = []
        self._session_established = False
        self._ping_task: asyncio.Task[None] | None = None
        self._receive_task: asyncio.Task[None] | None = None

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        token: str | None = None,
        on_closed_candle: ClosedCandleCallback | None = None,
    ) -> "XRocketWebSocketClient":
        return cls(
            url=settings.xrocket_ws_url,
            token=token,
            ping_interval=settings.ws_ping_interval,
            stale_seconds=settings.ws_stale_seconds,
            reconnect_base_seconds=settings.ws_reconnect_base_seconds,
            reconnect_max_seconds=settings.ws_reconnect_max_seconds,
            on_closed_candle=on_closed_candle,
        )

    async def run(self) -> None:
        self._running = True
        attempt = 0
        try:
            while self._running:
                self._session_established = False
                try:
                    await self._session_once()
                except _StopClient:
                    self._running = False
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if not self._running:
                        break
                    self._log.warning("websocket disconnected (%s)", type(exc).__name__)
                if not self._running:
                    break
                if self._session_established:
                    attempt = 0
                delay = self._reconnect_delay(attempt)
                attempt += 1
                await self._sleep(delay)
        finally:
            self._running = False
            self._connected = False
            await self._cleanup()

    async def stop(self) -> None:
        self._running = False
        self._fail_pending(ConnectionError("websocket stopped"))
        await self._close_socket()

    def subscribe_ticker(self, symbol: str, interval: str) -> None:
        if interval not in TICKER_INTERVALS:
            raise ValueError(f"unsupported ticker interval: {interval}")
        self._remember(
            _Subscription(
                channel="ticker",
                params={"channel": "ticker", "symbol": symbol, "interval": interval},
                stream_key=f"ticker:{symbol}",
            )
        )

    def subscribe_candles(
        self,
        symbol: str,
        interval: str,
        *,
        snapshot_start: datetime | None = None,
        snapshot_end: datetime | None = None,
    ) -> None:
        if interval not in CANDLE_INTERVALS:
            raise ValueError(f"unsupported candle interval: {interval}")
        params: dict[str, object] = {"channel": "candles", "symbol": symbol, "type": interval}
        if snapshot_start is not None and snapshot_end is not None:
            params["snapshot"] = {"startAt": snapshot_start.isoformat(), "endAt": snapshot_end.isoformat()}
        self._remember(_Subscription(channel="candles", params=params, stream_key=f"candles:{symbol}:{interval}"))

    def subscribe_orderbook(self, symbol: str, depth: int, precision: str | None = None) -> None:
        if depth not in ORDERBOOK_DEPTHS:
            raise ValueError(f"unsupported order book depth: {depth}")
        params: dict[str, object] = {"channel": "orderbook", "symbol": symbol, "depth": depth}
        if precision is not None:
            params["precision"] = precision
        self._remember(_Subscription(channel="orderbook", params=params, stream_key=f"orderbook:{symbol}"))

    def subscribe_trades(self, symbol: str) -> None:
        self._remember(
            _Subscription(
                channel="trades",
                params={"channel": "trades", "symbol": symbol},
                stream_key=f"trades:{symbol}",
            )
        )

    def subscribe_balances(self) -> None:
        if self._token is None:
            raise ValueError("balances requires an API token")
        self._remember(_Subscription(channel="balances", params={"channel": "balances"}, stream_key="balances"))

    def subscribe_active_orders(self) -> None:
        if self._token is None:
            raise ValueError("activeOrders requires an API token")
        self._remember(
            _Subscription(channel="activeOrders", params={"channel": "activeOrders"}, stream_key="activeOrders")
        )

    def is_stale(self, stream_key: str) -> bool:
        seen = self._seen_at.get(stream_key)
        if seen is None:
            return True
        return (self._clock() - seen) > self._stale_seconds

    def is_healthy(self) -> bool:
        if not self._connected:
            return False
        keys = [sub.stream_key for sub in self._subs.values()]
        if not keys:
            return not self.is_stale("connection")
        return all(not self.is_stale(key) for key in keys)

    def ticker(self, symbol: str) -> Ticker | None:
        return self._tickers.get(symbol)

    def orderbook(self, symbol: str) -> OrderBook | None:
        return self._books.get(symbol)

    def orderbook_is_reliable(self, symbol: str) -> bool:
        """Time-based staleness is separate. Increments are not merged."""
        return self._book_reliable.get(symbol, False)

    def latest_closed_candle(self, symbol: str, interval: str) -> Candle | None:
        return self._closed.get((symbol, interval))

    def last_trade(self, symbol: str) -> PublicTrade | None:
        return self._last_trade.get(symbol)

    def ws_balances(self) -> list[AssetBalance]:
        # TODO: which wallet this channel reports (trading, funding, or both) is NOT DOCUMENTED.
        return list(self._ws_balances)

    def active_orders(self) -> list[ExchangeOrder]:
        return list(self._active_orders)

    def _remember(self, subscription: _Subscription) -> None:
        self._subs[subscription.stream_key] = subscription

    def _reconnect_delay(self, attempt: int) -> float:
        power = min(attempt, 10)
        growth = self._reconnect_base * (2**power)
        return min(self._reconnect_max, growth) * self._jitter()

    async def _session_once(self) -> None:
        socket = await self._opener()
        self._socket = socket
        self._connected = True
        self._touch("connection")
        self._pending.clear()
        self._receive_task = asyncio.create_task(self._receive_loop())
        self._ping_task = asyncio.create_task(self._ping_loop())
        try:
            if self._token is not None:
                await self._authenticate()
            await self._resubscribe()
            self._session_established = True
            if self._receive_task is not None:
                await self._receive_task
        finally:
            self._connected = False
            await self._finish_task(self._ping_task)
            await self._finish_task(self._receive_task)
            await self._close_socket()

    async def _authenticate(self) -> None:
        request_id = self._next_id()
        # The auth frame contains the token. It is not logged.
        message = ws_auth_message(request_id, self._token or "")
        try:
            await self._send_and_wait(message)
        except (InvalidTokenError, UserBlockedError) as exc:
            raise _StopClient(str(exc)) from exc
        self._log.info("websocket authenticated")

    async def _resubscribe(self) -> None:
        for subscription in self._subs.values():
            request = {"id": self._next_id(), "method": "subscribe", "params": subscription.params}
            await self._send_and_wait(request)

    async def _ping_loop(self) -> None:
        while self._running and self._connected:
            await self._sleep(self._ping_interval)
            if not self._running or not self._connected or self._socket is None:
                return
            await self._socket.send_json({"id": self._next_id(), "method": "ping"})

    async def _receive_loop(self) -> None:
        socket = self._socket
        if socket is None:
            return
        while self._running:
            try:
                message = await socket.receive_json()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._fail_pending(exc)
                if not self._running:
                    return
                raise
            self._touch("connection")
            await self._dispatch(message)

    async def _dispatch(self, message: dict[str, object]) -> None:
        request_id = message.get("id")
        future = self._pending.pop(str(request_id), None) if request_id is not None else None
        error = _error_payload(message)
        if error is not None:
            mapped = _map_ws_error(error)
            if future is not None and not future.done():
                future.set_exception(mapped)
            if _error_code(error) in _FATAL_WS_CODES:
                self._running = False
                if future is None:
                    raise _StopClient(str(mapped))
            return
        try:
            await self._consume(message)
        except Exception:
            self._log.warning("skipped a market message")
        if future is not None and not future.done():
            future.set_result(message)

    async def _consume(self, message: dict[str, object]) -> None:
        params = message.get("params")
        result = message.get("result")
        channel: str | None = None
        data: object = None
        if isinstance(params, dict):
            raw_channel = params.get("channel")
            channel = str(raw_channel) if raw_channel is not None else None
            data = params.get("data")
        if data is None and isinstance(result, dict):
            # TODO: the snapshot field path inside the subscribe result is only partly documented.
            raw_channel = result.get("channel")
            if channel is None and raw_channel is not None:
                channel = str(raw_channel)
            nested = result.get("data")
            data = nested if isinstance(nested, dict) else result
        if not isinstance(data, dict):
            return
        channel = channel or _infer_channel(data)
        if channel == "ticker":
            self._take_ticker(data)
        elif channel == "candles":
            await self._take_candles(data)
        elif channel == "orderbook":
            self._take_orderbook(data)
        elif channel == "trades":
            self._take_trades(data)
        elif channel == "balances":
            self._take_balances(data)
        elif channel == "activeOrders":
            self._take_orders(data)

    def _take_ticker(self, data: dict[str, object]) -> None:
        raw = data.get("ticker")
        if isinstance(raw, dict):
            ticker = Ticker.model_validate(raw)
            self._tickers[ticker.symbol] = ticker
            self._touch(f"ticker:{ticker.symbol}")
            return
        raw_list = data.get("tickers")
        if isinstance(raw_list, list):
            for item in raw_list:
                if isinstance(item, dict):
                    ticker = Ticker.model_validate(item)
                    self._tickers[ticker.symbol] = ticker
                    self._touch(f"ticker:{ticker.symbol}")

    async def _take_candles(self, data: dict[str, object]) -> None:
        symbol, interval = self._candle_context(data)
        if symbol is None or interval is None:
            # TODO: a candle push does not document symbol/interval. More than one
            # candles subscription makes the row impossible to attribute.
            self._log.warning("candle message was not attributed to a subscription")
            return
        snapshot = data.get("candles")
        if isinstance(snapshot, list):
            candles = [Candle.from_array(row) for row in snapshot if isinstance(row, list)]
            candles.sort(key=lambda item: item.start)
            if not candles:
                return
            for candle in candles[:-1]:
                await self._accept_candle(symbol, interval, candle, closed=True)
            await self._accept_candle(symbol, interval, candles[-1], closed=False)
            return
        row = data.get("candle")
        if isinstance(row, list):
            await self._accept_candle(symbol, interval, Candle.from_array(row), closed=False)

    async def _accept_candle(self, symbol: str, interval: str, candle: Candle, *, closed: bool) -> None:
        self._touch(f"candles:{symbol}:{interval}")
        key = (symbol, interval)
        if closed:
            self._closed[key] = candle
            await self._emit_closed(candle, symbol, interval)
            return
        previous = self._forming.get(key)
        if previous is not None and previous.start < candle.start:
            self._closed[key] = previous
            await self._emit_closed(previous, symbol, interval)
        elif previous is not None and previous.start == candle.start:
            self._forming[key] = candle
            return
        self._forming[key] = candle

    async def _emit_closed(self, candle: Candle, symbol: str, interval: str) -> None:
        if self._on_closed_candle is None:
            return
        result = self._on_closed_candle(candle, symbol, interval)
        if result is not None:
            await result

    def _candle_context(self, data: dict[str, object]) -> tuple[str | None, str | None]:
        symbol = data.get("symbol")
        interval = data.get("type") or data.get("interval")
        if isinstance(symbol, str) and isinstance(interval, str):
            return symbol, interval
        matches = [sub for sub in self._subs.values() if sub.channel == "candles"]
        if len(matches) == 1:
            params = matches[0].params
            found_symbol = params.get("symbol")
            found_interval = params.get("type")
            if isinstance(found_symbol, str) and isinstance(found_interval, str):
                return found_symbol, found_interval
        return None, None

    def _take_orderbook(self, data: dict[str, object]) -> None:
        symbol = self._book_symbol(data)
        if symbol is None:
            self._log.warning("order book message was not attributed to a subscription")
            return
        self._touch(f"orderbook:{symbol}")
        snapshot_flag = data.get("snapshot")
        # TODO: how to apply an increment (absolute size vs delta, delete on zero)
        # is NOT DOCUMENTED. The last full snapshot is kept and marked unreliable.
        if snapshot_flag is False or (snapshot_flag is None and symbol in self._books):
            self._book_reliable[symbol] = False
            self._book_increment_pending[symbol] = True
            return
        try:
            book = OrderBook.from_payload(data)
        except (ValueError, KeyError):
            self._book_reliable[symbol] = False
            self._book_increment_pending[symbol] = True
            return
        self._books[symbol] = book
        self._book_reliable[symbol] = True
        self._book_increment_pending[symbol] = False

    def _book_symbol(self, data: dict[str, object]) -> str | None:
        symbol = data.get("symbol")
        if isinstance(symbol, str):
            return symbol
        matches = [sub for sub in self._subs.values() if sub.channel == "orderbook"]
        if len(matches) == 1:
            found = matches[0].params.get("symbol")
            if isinstance(found, str):
                return found
        return None

    def _take_trades(self, data: dict[str, object]) -> None:
        symbol = data.get("symbol")
        trades = data.get("trades")
        if not isinstance(trades, list) or not trades:
            return
        last = trades[-1]
        if not isinstance(last, dict):
            return
        trade = PublicTrade.model_validate(last)
        resolved = symbol if isinstance(symbol, str) else self._only_trade_symbol()
        if resolved is None:
            return
        self._last_trade[resolved] = trade
        self._touch(f"trades:{resolved}")

    def _only_trade_symbol(self) -> str | None:
        matches = [sub for sub in self._subs.values() if sub.channel == "trades"]
        if len(matches) != 1:
            return None
        found = matches[0].params.get("symbol")
        if isinstance(found, str):
            return found
        return None

    def _take_balances(self, data: dict[str, object]) -> None:
        raw = data.get("balances")
        if not isinstance(raw, list):
            return
        self._ws_balances = [AssetBalance.model_validate(item) for item in raw if isinstance(item, dict)]
        self._touch("balances")

    def _take_orders(self, data: dict[str, object]) -> None:
        raw = data.get("orders")
        if not isinstance(raw, list):
            return
        self._active_orders = [ExchangeOrder.model_validate(item) for item in raw if isinstance(item, dict)]
        self._touch("activeOrders")

    async def _send_and_wait(self, payload: dict[str, object]) -> dict[str, object]:
        socket = self._socket
        if socket is None:
            raise ConnectionError("websocket is not connected")
        request_id = str(payload["id"])
        future: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await socket.send_json(payload)
        return await future

    def _fail_pending(self, exc: BaseException) -> None:
        pending = list(self._pending.values())
        self._pending.clear()
        for future in pending:
            if not future.done():
                future.set_exception(exc)

    def _touch(self, stream_key: str) -> None:
        self._seen_at[stream_key] = self._clock()

    def _next_id(self) -> str:
        self._seq += 1
        return str(self._seq)

    async def _open_default(self) -> WebSocketLike:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession()
            self._owns_http = True
        # JSON ping is our keepalive. aiohttp's heartbeat would send a different ping.
        socket = await self._http.ws_connect(self._url, heartbeat=None, autoping=True)
        return socket

    async def _close_socket(self) -> None:
        socket = self._socket
        self._socket = None
        if socket is None:
            return
        try:
            await socket.close()
        except Exception:
            self._log.info("websocket close failed")

    async def _cleanup(self) -> None:
        await self._close_socket()
        if self._owns_http and self._http is not None and not self._http.closed:
            await self._http.close()

    async def _finish_task(self, task: asyncio.Task[None] | None) -> None:
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            return


def _default_jitter() -> float:
    return random.uniform(0.5, 1.0)


def _infer_channel(data: dict[str, object]) -> str | None:
    if "ticker" in data or "tickers" in data:
        return "ticker"
    if "candle" in data or "candles" in data:
        return "candles"
    if "bids" in data or "asks" in data:
        return "orderbook"
    if "trades" in data:
        return "trades"
    if "balances" in data:
        return "balances"
    if "orders" in data:
        return "activeOrders"
    return None


def _error_payload(message: dict[str, object]) -> dict[str, object] | None:
    error = message.get("error")
    if isinstance(error, dict):
        return error
    return None


def _error_code(error: dict[str, object]) -> int | None:
    code = error.get("code")
    if isinstance(code, int):
        return code
    return None


def _map_ws_error(error: dict[str, object]) -> Exception:
    code = _error_code(error)
    text = str(error.get("message") or code or "websocket error")
    if code == -32030:
        return InvalidTokenError(text)
    if code == -32034:
        return UserBlockedError(text)
    return ConnectionError(text)
