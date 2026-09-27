"""WebSocket ping, reconnect, staleness, and closed-candle callbacks."""

import asyncio
from collections.abc import Callable

import pytest

from xrocket.websocket_client import XRocketWebSocketClient

TICKER = {
    "symbol": "BTC-USDT",
    "startTime": "2026-09-27T00:00:00Z",
    "endTime": "2026-09-27T23:59:59Z",
    "open": "1",
    "close": "2",
    "high": "3",
    "low": "0.5",
    "changeRate": "0.024",
    "changePrice": "1",
    "baseVolume": "10",
    "quoteVolume": "20",
    "last": "2",
}


class ScriptedSocket:
    def __init__(
        self,
        *,
        fail_after_subscribes: int | None = None,
        on_subscribe: Callable[[dict], list] | None = None,
    ) -> None:
        self.sent: list[dict] = []
        self.fail_after_subscribes = fail_after_subscribes
        self._subscribe_count = 0
        self._on_subscribe = on_subscribe
        self._queue: asyncio.Queue[object] = asyncio.Queue()
        self.closed = False

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)
        method = data.get("method")
        if method in {"auth", "subscribe"}:
            await self._queue.put({"id": data["id"], "result": {"success": True}})
        if method == "subscribe":
            self._subscribe_count += 1
            if self._on_subscribe is not None and self._subscribe_count == 1:
                for item in self._on_subscribe(data):
                    await self._queue.put(item)
            if self.fail_after_subscribes is not None and self._subscribe_count >= self.fail_after_subscribes:
                await self._queue.put(ConnectionResetError("lost"))

    async def receive_json(self) -> dict:
        item = await self._queue.get()
        if isinstance(item, BaseException):
            raise item
        if not isinstance(item, dict):
            raise TypeError("scripted socket expected an object")
        return item

    async def close(self) -> None:
        self.closed = True
        await self._queue.put(ConnectionError("closed"))


def _instant_sleep(recorded: list[float], *, block_from: float = 1000):
    async def sleep(seconds: float) -> None:
        recorded.append(seconds)
        if seconds >= block_from:
            await asyncio.sleep(3600)
        else:
            await asyncio.sleep(0)

    return sleep


async def _stop(client: XRocketWebSocketClient, task: asyncio.Task[None]) -> None:
    await client.stop()
    await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
async def test_ping_is_sent_after_each_interval() -> None:
    socket = ScriptedSocket()
    sleeps: list[float] = []

    async def opener() -> ScriptedSocket:
        return socket

    client = XRocketWebSocketClient(
        "wss://exchange.example/ws",
        opener=opener,
        sleep=_instant_sleep(sleeps, block_from=10_000),
        ping_interval=30,
        jitter=lambda: 1,
    )
    task = asyncio.create_task(client.run())
    try:
        for _ in range(50):
            await asyncio.sleep(0)
            if any(item.get("method") == "ping" for item in socket.sent):
                break
        else:
            raise AssertionError(socket.sent)
    finally:
        await _stop(client, task)
    ping = next(item for item in socket.sent if item.get("method") == "ping")
    assert "id" in ping
    assert "params" not in ping
    assert 30 in sleeps


@pytest.mark.asyncio
async def test_reconnect_backoff_grows_until_the_socket_connects() -> None:
    sleeps: list[float] = []
    sockets: list[ScriptedSocket] = []
    attempts = {"n": 0}

    async def opener() -> ScriptedSocket:
        attempts["n"] += 1
        if attempts["n"] < 4:
            raise ConnectionError("connect failed")
        socket = ScriptedSocket()
        sockets.append(socket)
        return socket

    client = XRocketWebSocketClient(
        "wss://exchange.example/ws",
        opener=opener,
        sleep=_instant_sleep(sleeps),
        ping_interval=10_000,
        reconnect_base_seconds=1,
        reconnect_max_seconds=30,
        jitter=lambda: 1,
    )
    task = asyncio.create_task(client.run())
    try:
        for _ in range(50):
            await asyncio.sleep(0)
            if sockets:
                break
        else:
            raise AssertionError(sleeps)
    finally:
        await _stop(client, task)
    assert sleeps[:3] == [1.0, 2.0, 4.0]
    assert client._reconnect_delay(0) == 1.0
    client._jitter = lambda: 0.5
    assert client._reconnect_delay(0) == 0.5
    assert client._reconnect_delay(2) == 2.0


@pytest.mark.asyncio
async def test_subscriptions_are_sent_again_after_reconnect(caplog) -> None:
    sleeps: list[float] = []
    sockets: list[ScriptedSocket] = []

    async def opener() -> ScriptedSocket:
        socket = ScriptedSocket(fail_after_subscribes=2 if len(sockets) == 0 else None)
        sockets.append(socket)
        return socket

    client = XRocketWebSocketClient(
        "wss://exchange.example/ws",
        token="ws-token-value",
        opener=opener,
        sleep=_instant_sleep(sleeps),
        ping_interval=10_000,
        reconnect_base_seconds=1,
        reconnect_max_seconds=30,
        jitter=lambda: 1,
    )
    client.subscribe_ticker("BTC-USDT", "15min")
    client.subscribe_candles("BTC-USDT", "15min")
    task = asyncio.create_task(client.run())
    try:
        for _ in range(80):
            await asyncio.sleep(0)
            if len(sockets) >= 2:
                sent_subs = [item for item in sockets[1].sent if item.get("method") == "subscribe"]
                if len(sent_subs) >= 2:
                    break
        else:
            raise AssertionError([item.get("method") for socket in sockets for item in socket.sent])
    finally:
        await _stop(client, task)

    first_methods = [item.get("method") for item in sockets[0].sent]
    second_methods = [item.get("method") for item in sockets[1].sent]
    assert first_methods[:3] == ["auth", "subscribe", "subscribe"]
    assert second_methods[:3] == ["auth", "subscribe", "subscribe"]
    assert sockets[1].sent[0]["params"]["token"] == "ws-token-value"
    assert [item["params"]["channel"] for item in sockets[1].sent if item["method"] == "subscribe"] == [
        "ticker",
        "candles",
    ]
    assert 1.0 in sleeps
    assert "ws-token-value" not in caplog.text


@pytest.mark.asyncio
async def test_stream_becomes_stale_when_messages_stop() -> None:
    now = {"t": 0.0}
    socket = ScriptedSocket(
        on_subscribe=lambda data: [
            {
                "method": "subscription",
                "params": {"channel": "ticker", "data": {"ticker": TICKER}},
            }
        ]
        if data["params"]["channel"] == "ticker"
        else []
    )

    async def opener() -> ScriptedSocket:
        return socket

    client = XRocketWebSocketClient(
        "wss://exchange.example/ws",
        opener=opener,
        sleep=_instant_sleep([]),
        clock=lambda: now["t"],
        ping_interval=10_000,
        stale_seconds=90,
        jitter=lambda: 1,
    )
    client.subscribe_ticker("BTC-USDT", "1min")
    task = asyncio.create_task(client.run())
    try:
        for _ in range(50):
            await asyncio.sleep(0)
            if client.is_healthy():
                break
        else:
            raise AssertionError(socket.sent)
        assert client.ticker("BTC-USDT") is not None
        assert client.ticker("BTC-USDT").last == 2
        now["t"] = 90
        assert client.is_stale("ticker:BTC-USDT") is False
        now["t"] = 90.1
        assert client.is_stale("ticker:BTC-USDT") is True
        assert client.is_healthy() is False
    finally:
        await _stop(client, task)
    assert client.is_healthy() is False


@pytest.mark.asyncio
async def test_closed_candle_callback_runs_only_when_a_candle_finishes() -> None:
    closed: list[str] = []

    async def on_closed(candle, symbol: str, interval: str) -> None:
        closed.append(f"{symbol}:{interval}:{candle.close}")

    def pushes(data: dict) -> list[dict]:
        del data
        rows = [
            ("2026-09-27T00:00:00Z", "10"),
            ("2026-09-27T00:00:00Z", "11"),
            ("2026-09-27T00:15:00Z", "20"),
        ]
        return [
            {
                "method": "subscription",
                "params": {"channel": "candles", "data": {"candle": [start, "1", close, "9", "0.5", "8"]}},
            }
            for start, close in rows
        ]

    socket = ScriptedSocket(on_subscribe=pushes)

    async def opener() -> ScriptedSocket:
        return socket

    client = XRocketWebSocketClient(
        "wss://exchange.example/ws",
        opener=opener,
        sleep=_instant_sleep([]),
        ping_interval=10_000,
        on_closed_candle=on_closed,
        jitter=lambda: 1,
    )
    client.subscribe_candles("BTC-USDT", "15min")
    task = asyncio.create_task(client.run())
    try:
        for _ in range(50):
            await asyncio.sleep(0)
            if closed:
                break
        else:
            raise AssertionError(socket.sent)
        await asyncio.sleep(0)
    finally:
        await _stop(client, task)
    assert closed == ["BTC-USDT:15min:11"]
    latest = client.latest_closed_candle("BTC-USDT", "15min")
    assert latest is not None
    assert latest.close == 11
    assert latest.high == 9


@pytest.mark.asyncio
async def test_candle_snapshot_callbacks_skip_the_still_open_bar() -> None:
    closed: list[str] = []

    def on_closed(candle, symbol: str, interval: str) -> None:
        del symbol, interval
        closed.append(str(candle.close))

    socket = ScriptedSocket()

    async def send_json(data: dict) -> None:
        socket.sent.append(data)
        if data.get("method") == "subscribe":
            await socket._queue.put(
                {
                    "id": data["id"],
                    "result": {
                        "success": True,
                        "channel": "candles",
                        "data": {
                            "candles": [
                                ["2026-09-27T00:00:00Z", "1", "2", "4", "0.5", "8"],
                                ["2026-09-27T00:15:00Z", "2", "3", "5", "1", "8"],
                                ["2026-09-27T00:30:00Z", "3", "4", "6", "2", "8"],
                            ]
                        },
                    },
                }
            )

    socket.send_json = send_json  # type: ignore[method-assign]

    async def opener() -> ScriptedSocket:
        return socket

    client = XRocketWebSocketClient(
        "wss://exchange.example/ws",
        opener=opener,
        sleep=_instant_sleep([]),
        ping_interval=10_000,
        on_closed_candle=on_closed,
        jitter=lambda: 1,
    )
    client.subscribe_candles("ETH-USDT", "15min")
    task = asyncio.create_task(client.run())
    try:
        for _ in range(50):
            await asyncio.sleep(0)
            if len(closed) >= 2:
                break
        else:
            raise AssertionError(closed)
    finally:
        await _stop(client, task)
    assert closed == ["2", "3"]
    latest = client.latest_closed_candle("ETH-USDT", "15min")
    assert latest is not None
    assert latest.close == 3


@pytest.mark.asyncio
async def test_orderbook_increments_are_not_merged() -> None:
    books = [
        {
            "method": "subscription",
            "params": {
                "channel": "orderbook",
                "data": {
                    "sequence": "1",
                    "snapshot": True,
                    "bids": [["10", "1"]],
                    "asks": [["11", "2"]],
                    "bidTotalAmount": "1",
                    "askTotalAmount": "2",
                },
            },
        },
        {
            "method": "subscription",
            "params": {
                "channel": "orderbook",
                "data": {
                    "sequence": "2",
                    "snapshot": False,
                    "bids": [["10", "0"]],
                    "asks": [["11", "9"]],
                    "bidTotalAmount": "0",
                    "askTotalAmount": "9",
                },
            },
        },
    ]

    socket = ScriptedSocket(on_subscribe=lambda data: books)

    async def opener() -> ScriptedSocket:
        return socket

    client = XRocketWebSocketClient(
        "wss://exchange.example/ws",
        opener=opener,
        sleep=_instant_sleep([]),
        ping_interval=10_000,
        jitter=lambda: 1,
    )
    client.subscribe_orderbook("BTC-USDT", 20)
    task = asyncio.create_task(client.run())
    try:
        for _ in range(50):
            await asyncio.sleep(0)
            if client.orderbook("BTC-USDT") is not None and client.orderbook_is_reliable("BTC-USDT") is False:
                break
        else:
            raise AssertionError((client.orderbook("BTC-USDT"), client.orderbook_is_reliable("BTC-USDT")))
    finally:
        await _stop(client, task)
    book = client.orderbook("BTC-USDT")
    assert book is not None
    assert book.bids[0].size == 1
    assert client.orderbook_is_reliable("BTC-USDT") is False
