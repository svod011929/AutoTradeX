"""REST client behavior against a local HTTP server.

aioresponses 0.7 cannot build responses on the installed aiohttp, so these
tests speak HTTP to an in-process server instead.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from aiohttp import web

from core.config import Settings
from xrocket.exceptions import (
    DuplicateClientOrderIdError,
    InsufficientBalanceError,
    InvalidSymbolError,
    InvalidTokenError,
    OrderNotFoundError,
    OrdersDisabledError,
    PrecisionError,
    RateLimitError,
    UnknownOrderResultError,
    parse_retry_after,
)
from xrocket.models import Symbol
from xrocket.precision import floor_funds, floor_price, floor_size, format_decimal, quote_step
from xrocket.rate_limit import ClientRateLimiter
from xrocket.rest_client import XRocketRestClient, page_limit_seconds

TOKEN = "test-token-value"

SYMBOL = {
    "symbol": "BTC-USDT",
    "baseAsset": "BTC",
    "quoteAsset": "USDT",
    "baseMinSize": "0.00001",
    "quoteMinSize": "0.01",
    "baseMaxSize": "1000",
    "quoteMaxSize": "1000000",
    "minPrice": "0.01",
    "maxPrice": "1000000",
    "baseIncrement": "0.00001",
    "priceIncrement": "0.01",
    "enableTrading": True,
    "precisions": ["0.01"],
    "labels": [],
}


def order_body() -> dict[str, str]:
    return {
        "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "clientOrderId": "abcDEF_01",
        "symbol": "BTC-USDT",
        "side": "buy",
        "status": "completed",
        "createdAt": "2026-09-27T00:00:00Z",
        "updatedAt": "2026-09-27T00:00:01Z",
        "dealSize": "0.01",
        "dealFunds": "100",
        "fee": "1",
        "feeAsset": "USDT",
        "timeInForce": "IOC",
        "type": "market",
        "funds": "100",
    }


class Exchange:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self._plan: list[dict[str, object]] = []

    def add(
        self,
        *,
        status: int = 200,
        payload: object | None = None,
        headers: dict[str, str] | None = None,
        mode: str = "json",
    ) -> None:
        self._plan.append({"status": status, "payload": payload, "headers": headers or {}, "mode": mode})

    async def handle(self, request: web.Request) -> web.Response:
        raw = await request.read()
        parsed: object = None
        if raw:
            parsed = json.loads(raw)
        self.calls.append(
            {
                "method": request.method,
                "path": request.path,
                "headers": {key.lower(): value for key, value in request.headers.items()},
                "query": {key: request.query.getall(key) for key in request.query.keys()},
                "json": parsed,
            }
        )
        item = self._plan.pop(0)
        mode = str(item["mode"])
        if mode == "hang":
            await self._until_disconnected(request)
            return web.Response(status=504)
        if mode == "drop":
            transport = request.transport
            if transport is not None:
                transport.close()
            await self._until_disconnected(request)
            return web.Response(status=500)
        payload = item["payload"] if item["payload"] is not None else {}
        return web.Response(
            status=int(item["status"]),
            body=json.dumps(payload).encode(),
            content_type="application/json",
            headers=item["headers"],  # type: ignore[arg-type]
        )

    async def _until_disconnected(self, request: web.Request) -> None:
        for _ in range(200):
            transport = request.transport
            if transport is None or transport.is_closing():
                return
            await asyncio.sleep(0.01)


@asynccontextmanager
async def serve(exchange: Exchange) -> AsyncIterator[str]:
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", exchange.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    host, port = runner.addresses[0]
    try:
        yield f"http://{host}:{port}"
    finally:
        await runner.cleanup()


def client(base: str, **kwargs: object) -> XRocketRestClient:
    values: dict[str, object] = {
        "token": TOKEN,
        "timeout_seconds": 1,
        "max_retries": 3,
        "max_rps": 1000,
        "allow_order_placement": True,
    }
    values.update(kwargs)
    return XRocketRestClient(base, **values)  # type: ignore[arg-type]


def test_precision_helpers_follow_symbol_increments() -> None:
    symbol = Symbol.model_validate(SYMBOL)
    assert format_decimal(Decimal("1E+2")) == "100"
    assert format_decimal(Decimal("0.0100")) == "0.01"
    assert floor_price(Decimal("1.239"), symbol) == Decimal("1.23")
    assert floor_size(Decimal("0.000019"), symbol) == Decimal("0.00001")
    assert quote_step(symbol.quote_min_size) == Decimal("0.01")
    assert floor_funds(Decimal("10.239"), symbol) == Decimal("10.23")
    with pytest.raises(PrecisionError):
        floor_size(Decimal("0.000001"), symbol)


def test_retry_after_parses_seconds_and_http_date() -> None:
    assert parse_retry_after("2") == 2
    moment = datetime(2026, 9, 27, 0, 0, tzinfo=timezone.utc)
    later = "Sun, 27 Sep 2026 00:00:05 GMT"
    assert parse_retry_after(later, now=moment) == 5


@pytest.mark.asyncio
async def test_public_requests_omit_the_auth_header_and_private_requests_send_it(caplog) -> None:
    exchange = Exchange()
    exchange.add(payload={"symbols": [SYMBOL]})
    exchange.add(payload={"balances": [{"asset": "USDT", "balance": "10", "available": "8", "holds": "2"}]})
    async with serve(exchange) as base:
        async with client(base) as api:
            symbols = await api.get_symbols()
            balances = await api.get_trading_balances()
    assert symbols[0].symbol == "BTC-USDT"
    assert balances[0].available == Decimal("8")
    assert "authorization" not in exchange.calls[0]["headers"]
    headers = exchange.calls[1]["headers"]
    assert isinstance(headers, dict)
    assert headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in caplog.text
    assert "Authorization" not in caplog.text


@pytest.mark.asyncio
async def test_error_mapping_covers_token_symbol_precision_balance_and_missing_order() -> None:
    cases = [
        (401, {"type": "/api/problems/unauthorized", "detail": "bad token"}, InvalidTokenError),
        (404, {"type": "/api/problems/exchange_symbol_not_found", "detail": "missing"}, InvalidSymbolError),
        (400, {"type": "/api/problems/exchange_incorrect_decimal_places", "detail": "decimals"}, PrecisionError),
        (
            400,
            {"type": "/api/problems/exchange_amount_more_than_user_balance", "detail": "balance"},
            InsufficientBalanceError,
        ),
        (404, {"type": "/api/problems/exchange_order_not_found", "detail": "gone"}, OrderNotFoundError),
    ]
    for status, payload, expected in cases:
        exchange = Exchange()
        exchange.add(status=status, payload=payload)
        async with serve(exchange) as base:
            async with client(base) as api:
                with pytest.raises(expected):
                    await api.get_order(order_id="order-1")
        assert len(exchange.calls) == 1


@pytest.mark.asyncio
async def test_rate_limit_honors_retry_after_then_succeeds() -> None:
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    exchange = Exchange()
    exchange.add(
        status=429,
        headers={"Retry-After": "2"},
        payload={"type": "/api/problems/rate_limit", "status": 429, "detail": "slow down"},
    )
    exchange.add(payload={"symbols": [SYMBOL]})
    async with serve(exchange) as base:
        async with client(base, sleep=sleep) as api:
            symbols = await api.get_symbols()
    assert symbols[0].base_asset == "BTC"
    assert 2 in sleeps
    assert len(exchange.calls) == 2


@pytest.mark.asyncio
async def test_create_order_timeout_is_unknown_and_is_not_retried() -> None:
    exchange = Exchange()
    exchange.add(mode="hang")
    async with serve(exchange) as base:
        async with client(base, timeout_seconds=0.2, max_retries=5) as api:
            with pytest.raises(UnknownOrderResultError, match="unknown"):
                await api.create_market_entry("BTC-USDT", "buy", "25", "abcDEF_01")
    assert len(exchange.calls) == 1
    assert exchange.calls[0]["method"] == "POST"


@pytest.mark.asyncio
async def test_create_order_connection_loss_and_server_error_are_not_retried() -> None:
    dropped = Exchange()
    dropped.add(mode="drop")
    async with serve(dropped) as base:
        async with client(base, timeout_seconds=1, max_retries=4) as api:
            with pytest.raises(UnknownOrderResultError):
                await api.create_market_entry("BTC-USDT", "buy", "25", "abcDEF_01")
    assert len(dropped.calls) == 1

    failed = Exchange()
    failed.add(status=500, payload={"type": "/api/problems/internal_error", "detail": "down"})
    async with serve(failed) as base:
        async with client(base, max_retries=4) as api:
            with pytest.raises(UnknownOrderResultError):
                await api.create_market_entry("BTC-USDT", "buy", "25", "abcDEF_01")
    assert len(failed.calls) == 1


@pytest.mark.asyncio
async def test_duplicate_client_order_id_and_rate_limit_do_not_replay_the_post() -> None:
    duplicate = Exchange()
    duplicate.add(
        status=400,
        payload={"type": "/api/problems/exchange_client_order_id_duplicate", "detail": "duplicate"},
    )
    async with serve(duplicate) as base:
        async with client(base) as api:
            with pytest.raises(DuplicateClientOrderIdError):
                await api.create_market_entry("BTC-USDT", "buy", "25", "abcDEF_01")
    assert len(duplicate.calls) == 1

    limited = Exchange()
    limited.add(status=429, headers={"Retry-After": "3"}, payload={"detail": "slow"})
    async with serve(limited) as base:
        async with client(base) as api:
            with pytest.raises(RateLimitError):
                await api.create_market_entry("BTC-USDT", "buy", "25", "abcDEF_01")
    assert len(limited.calls) == 1


@pytest.mark.asyncio
async def test_market_entry_sends_funds_ioc_and_client_order_id() -> None:
    exchange = Exchange()
    exchange.add(status=201, payload=order_body())
    async with serve(exchange) as base:
        async with client(base) as api:
            order = await api.create_market_entry("BTC-USDT", "buy", Decimal("25.50"), "abcDEF_01")
    body = exchange.calls[0]["json"]
    assert isinstance(body, dict)
    assert body["type"] == "market"
    assert body["funds"] == "25.5"
    assert body["timeInForce"] == "IOC"
    assert body["clientOrderId"] == "abcDEF_01"
    assert "size" not in body
    assert order.average_price == Decimal("10000")
    assert order.filled_quantity == Decimal("0.01")


@pytest.mark.asyncio
async def test_market_exit_sends_size_ioc_and_not_funds() -> None:
    exchange = Exchange()
    body = order_body()
    body["side"] = "sell"
    body["size"] = "0.5"
    body.pop("funds", None)
    exchange.add(status=201, payload=body)
    async with serve(exchange) as base:
        async with client(base) as api:
            await api.create_market_exit("BTC-USDT", Decimal("0.50"), "abcDEF_01")
    sent = exchange.calls[0]["json"]
    assert isinstance(sent, dict)
    assert sent["side"] == "sell"
    assert sent["type"] == "market"
    assert sent["size"] == "0.5"
    assert sent["timeInForce"] == "IOC"
    assert sent["clientOrderId"] == "abcDEF_01"
    assert "funds" not in sent


@pytest.mark.asyncio
async def test_disabled_execution_does_not_post() -> None:
    settings = Settings(_env_file=None, execution_mode="paper", allow_exchange_orders=True)
    exchange = Exchange()
    async with serve(exchange) as base:
        async with XRocketRestClient.from_settings(settings, token=TOKEN) as api:
            api._base_url = base
            with pytest.raises(OrdersDisabledError):
                await api.create_market_entry("BTC-USDT", "buy", "1", "abcDEF_01")
    assert exchange.calls == []


@pytest.mark.asyncio
async def test_get_order_by_client_order_id_and_prefers_order_id() -> None:
    exchange = Exchange()
    exchange.add(payload=order_body())
    exchange.add(payload=order_body())
    async with serve(exchange) as base:
        async with client(base) as api:
            await api.get_order(client_order_id="abcDEF_01")
            await api.get_order(order_id="order-1", client_order_id="abcDEF_01")
    assert exchange.calls[0]["query"] == {"clientOrderId": ["abcDEF_01"]}
    assert exchange.calls[1]["query"] == {"orderId": ["order-1"]}


@pytest.mark.asyncio
async def test_candle_rows_use_documented_field_order_and_page_on_max_interval() -> None:
    start = datetime(2026, 9, 27, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=2)
    exchange = Exchange()

    async def candles(request: web.Request) -> web.Response:
        raw = await request.read()
        exchange.calls.append({"query": {key: request.query.getall(key) for key in request.query.keys()}, "body": raw})
        window = (request.query["startAt"], request.query["endAt"])
        if window == (start.isoformat(), end.isoformat()):
            return web.json_response(
                {
                    "type": "/api/problems/exchange_too_big_interval_validation",
                    "detail": "too big",
                    "info": {"maxIntervalInSeconds": 3600},
                },
                status=400,
            )
        close = "4" if request.query["startAt"] == start.isoformat() else "7"
        return web.json_response({"candles": [[request.query["startAt"], "1", close, "9", "0.5", "8"]]})

    app = web.Application()
    app.router.add_get("/api/v1/candles", candles)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    host, port = runner.addresses[0]
    try:
        async with client(f"http://{host}:{port}") as api:
            rows = await api.get_candles("BTC-USDT", "15min", start, end)
    finally:
        await runner.cleanup()
    assert len(exchange.calls) == 3
    assert [candle.close for candle in rows] == [Decimal("4"), Decimal("7")]
    assert rows[0].high == Decimal("9")
    assert rows[0].low == Decimal("0.5")
    assert rows[0].open == Decimal("1")
    assert rows[0].base_volume == Decimal("8")


def test_page_limit_keeps_seconds_and_scales_a_millisecond_cap() -> None:
    assert page_limit_seconds(3600, 7200) == 3600
    assert page_limit_seconds(259200000, 5 * 86400) == 259200


@pytest.mark.asyncio
async def test_client_side_limiter_spaces_calls() -> None:
    now = {"t": 0.0}
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now["t"] += seconds

    limiter = ClientRateLimiter(2, sleep, lambda: now["t"])
    await limiter.acquire()
    await limiter.acquire()
    await limiter.acquire()
    assert sleeps == [0.5, 0.5]


def test_rest_client_has_no_transfer_or_withdrawal() -> None:
    assert not hasattr(XRocketRestClient, "transfer")
    assert not hasattr(XRocketRestClient, "withdraw")
    assert not hasattr(XRocketRestClient, "create_withdrawal")
