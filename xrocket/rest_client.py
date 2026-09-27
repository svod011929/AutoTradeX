"""xRocket REST client.

Paths and error types come from docs/XROCKET_API.md. Undocumented behavior
stays behind an explicit TODO. A lost create-order response is never retried.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta
from decimal import Decimal

import aiohttp

from core.config import Settings
from core.trading_policy import MARKET_TIME_IN_FORCE, fee_rate_to_fraction, validate_client_order_id
from xrocket.authentication import build_private_headers, build_public_headers
from xrocket.exceptions import (
    IntervalTooBigError,
    OrdersDisabledError,
    RateLimitError,
    UnknownOrderResultError,
    XRocketError,
    XRocketServerError,
    map_http_error,
)
from xrocket.models import (
    AssetBalance,
    CancelResult,
    Candle,
    ExchangeOrder,
    OrderBook,
    OrderHistoryPage,
    Symbol,
    Ticker,
    TradeFee,
)
from xrocket.precision import format_decimal
from xrocket.rate_limit import ClientRateLimiter

_CANDLE_INTERVALS = frozenset(
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
        "1week",
        "1month",
    }
)
_ORDERBOOK_DEPTHS = frozenset({5, 10, 20, 50, 100, 200, 500})
_MAX_CANDLE_PAGES = 5000
_MONEY_FIELDS = ("size", "funds", "price", "stopPrice")
_TRANSPORT = (
    TimeoutError,
    aiohttp.ServerTimeoutError,
    aiohttp.ClientConnectionError,
    aiohttp.ClientPayloadError,
)


class XRocketRestClient:
    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        *,
        timeout_seconds: float = 10,
        max_retries: int = 3,
        max_rps: float = 2,
        allow_order_placement: bool = False,
        fee_rate_is_fraction: bool = True,
        session: aiohttp.ClientSession | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token.strip() if token else None
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._max_retries = max_retries
        self._allow_order_placement = allow_order_placement
        self._fee_rate_is_fraction = fee_rate_is_fraction
        self._session = session
        self._owns_session = session is None
        self._sleep = sleep or asyncio.sleep
        self._limiter = ClientRateLimiter(max_rps, self._sleep, clock)
        self._log = logging.getLogger("autotrade.xrocket.rest")
        self._last_mapped: XRocketError | None = None

    @classmethod
    def from_settings(cls, settings: Settings, token: str | None = None) -> "XRocketRestClient":
        return cls(
            base_url=settings.xrocket_rest_url,
            token=token,
            timeout_seconds=settings.rest_timeout_seconds,
            max_retries=settings.max_retries,
            max_rps=settings.rest_max_rps,
            allow_order_placement=settings.exchange_orders_allowed(),
            fee_rate_is_fraction=settings.fee_rate_is_fraction,
        )

    async def __aenter__(self) -> "XRocketRestClient":
        await self._ensure_session()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()

    def fee_fraction(self, fee: TradeFee) -> Decimal:
        return fee_rate_to_fraction(fee.standard.taker, self._fee_rate_is_fraction)

    async def get_symbols(self) -> list[Symbol]:
        body = await self._request("GET", "/api/v1/symbols", private=False, retry=True)
        return [Symbol.model_validate(item) for item in _list_field(body, "symbols")]

    async def get_symbol(self, symbol: str) -> Symbol:
        body = await self._request("GET", f"/api/v1/symbols/{symbol}", private=False, retry=True)
        return Symbol.model_validate(body)

    async def get_ticker(self, symbols: list[str] | None = None) -> list[Ticker]:
        # TODO: the symbols query array wire format (csv vs repeated keys) is NOT DOCUMENTED.
        # Repeated keys are sent: symbols=BTC-USDT&symbols=ETH-USDT.
        body = await self._request(
            "GET",
            "/api/v1/ticker/24h",
            private=False,
            params=_repeated("symbols", symbols),
            retry=True,
        )
        return [Ticker.model_validate(item) for item in _list_field(body, "tickers")]

    async def get_candles(
        self,
        symbol: str,
        interval: str,
        start_at: datetime,
        end_at: datetime,
    ) -> list[Candle]:
        if interval not in _CANDLE_INTERVALS:
            raise ValueError(f"unsupported candle interval: {interval}")
        if end_at < start_at:
            raise ValueError("end_at is earlier than start_at")
        collected: dict[datetime, Candle] = {}
        try:
            rows = await self._fetch_candle_page(symbol, interval, start_at, end_at)
        except IntervalTooBigError as exc:
            collected = await self._page_candles(symbol, interval, start_at, end_at, exc)
        else:
            _store_candles(collected, rows)
        return [collected[key] for key in sorted(collected)]

    async def get_orderbook(
        self,
        symbol: str,
        depth: int | None = None,
        precision: str | None = None,
    ) -> OrderBook:
        if depth is not None and depth not in _ORDERBOOK_DEPTHS:
            raise ValueError(f"unsupported order book depth: {depth}")
        params: dict[str, str | int] = {"symbol": symbol}
        if depth is not None:
            params["depth"] = depth
        if precision is not None:
            params["precision"] = precision
        body = await self._request("GET", "/api/v1/orderbook", private=False, params=params, retry=True)
        return OrderBook.from_payload(body)

    async def get_trade_fees(self, symbols: list[str] | None = None) -> list[TradeFee]:
        # TODO: symbols array wire format is NOT DOCUMENTED (same as ticker). Repeated keys.
        # TODO: the fee unit is NOT DOCUMENTED. fee_rate_is_fraction controls how "0.01" is read.
        body = await self._request(
            "GET",
            "/api/v1/trade-fees",
            private=True,
            params=_repeated("symbols", symbols),
            retry=True,
        )
        return [TradeFee.model_validate(item) for item in _list_field(body, "fees")]

    async def get_trading_balances(self) -> list[AssetBalance]:
        body = await self._request("GET", "/api/v1/accounts/trading/balances", private=True, retry=True)
        return [AssetBalance.model_validate(item) for item in _list_field(body, "balances")]

    async def get_funding_balances(self) -> list[AssetBalance]:
        body = await self._request("GET", "/api/v1/accounts/funding/balances", private=True, retry=True)
        return [AssetBalance.model_validate(item) for item in _list_field(body, "balances")]

    async def create_market_entry(
        self,
        symbol: str,
        side: str,
        funds: Decimal | str,
        client_order_id: str,
    ) -> ExchangeOrder:
        """Market entry in quote funds with IOC. Store ``client_order_id`` before calling."""
        if side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        payload = {
            "symbol": symbol,
            "side": side,
            "type": "market",
            "funds": format_decimal(Decimal(str(funds))),
            "timeInForce": MARKET_TIME_IN_FORCE,
            "clientOrderId": client_order_id,
        }
        return await self.create_order(payload)

    async def create_market_exit(self, symbol: str, size: Decimal | str, client_order_id: str) -> ExchangeOrder:
        """Market SELL of an exact base size. Store ``client_order_id`` before calling."""
        payload = {
            "symbol": symbol,
            "side": "sell",
            "type": "market",
            "size": format_decimal(Decimal(str(size))),
            "timeInForce": MARKET_TIME_IN_FORCE,
            "clientOrderId": client_order_id,
        }
        return await self.create_order(payload)

    async def create_order(self, payload: Mapping[str, object]) -> ExchangeOrder:
        """POST /api/v1/orders. The caller stores clientOrderId before this call.

        Timeout, connection loss, and HTTP 5xx raise UnknownOrderResultError.
        The POST is not sent again.
        """
        self._require_orders_allowed()
        client_order_id = payload.get("clientOrderId")
        if not isinstance(client_order_id, str) or client_order_id == "":
            raise ValueError("clientOrderId is required and must be stored before the request")
        validate_client_order_id(client_order_id)
        body = await self._request(
            "POST",
            "/api/v1/orders",
            private=True,
            json_body=_stringify_order(payload),
            retry=False,
            order_create=True,
        )
        return ExchangeOrder.model_validate(body)

    async def cancel_order(
        self,
        *,
        order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> CancelResult:
        body = await self._request(
            "DELETE",
            "/api/v1/order",
            private=True,
            params=_order_lookup(order_id, client_order_id),
            retry=True,
        )
        return CancelResult.model_validate(body)

    async def get_order(
        self,
        *,
        order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> ExchangeOrder:
        body = await self._request(
            "GET",
            "/api/v1/order",
            private=True,
            params=_order_lookup(order_id, client_order_id),
            retry=True,
        )
        return ExchangeOrder.model_validate(body)

    async def get_active_orders(self) -> list[ExchangeOrder]:
        body = await self._request("GET", "/api/v1/orders/active", private=True, retry=True)
        return [ExchangeOrder.model_validate(item) for item in _list_field(body, "orders")]

    async def get_order_history(
        self,
        *,
        symbol: str | None = None,
        side: str | None = None,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        current_page: int | None = None,
        page_size: int | None = None,
        hide_canceled: bool | None = None,
    ) -> OrderHistoryPage:
        if page_size is not None and (page_size < 1 or page_size > 100):
            raise ValueError("pageSize must be from 1 to 100")
        if side is not None and side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        params: dict[str, str | int] = {}
        if symbol is not None:
            params["symbol"] = symbol
        if side is not None:
            params["side"] = side
        if start_at is not None:
            params["startAt"] = start_at.isoformat()
        if end_at is not None:
            params["endAt"] = end_at.isoformat()
        if current_page is not None:
            params["currentPage"] = current_page
        if page_size is not None:
            params["pageSize"] = page_size
        if hide_canceled is not None:
            params["hideCanceled"] = "true" if hide_canceled else "false"
        body = await self._request(
            "GET",
            "/api/v1/orders/history",
            private=True,
            params=params or None,
            retry=True,
        )
        return OrderHistoryPage.model_validate(body)

    def _require_orders_allowed(self) -> None:
        if not self._allow_order_placement:
            raise OrdersDisabledError(
                "exchange orders are disabled; execution mode is paper or the manual flag is off"
            )

    async def _page_candles(
        self,
        symbol: str,
        interval: str,
        start_at: datetime,
        end_at: datetime,
        exc: IntervalTooBigError,
    ) -> dict[datetime, Candle]:
        if exc.max_interval_seconds is None or exc.max_interval_seconds <= 0:
            raise exc
        if end_at <= start_at:
            raise exc
        span = (end_at - start_at).total_seconds()
        try:
            max_seconds = page_limit_seconds(exc.max_interval_seconds, span)
        except ValueError:
            raise exc from None
        pages = int(span // max_seconds) + (1 if span % max_seconds else 0)
        if pages > _MAX_CANDLE_PAGES:
            raise XRocketError(f"candle window needs {pages} pages; narrow the range") from exc
        collected: dict[datetime, Candle] = {}
        cursor = start_at
        while cursor < end_at:
            page_end = min(end_at, cursor + timedelta(seconds=max_seconds))
            if page_end <= cursor:
                raise XRocketError("candle page did not advance") from exc
            page_rows = await self._fetch_candle_page(symbol, interval, cursor, page_end)
            _store_candles(collected, page_rows)
            cursor = page_end
        return collected

    async def _fetch_candle_page(
        self,
        symbol: str,
        interval: str,
        start_at: datetime,
        end_at: datetime,
    ) -> list[object]:
        body = await self._request(
            "GET",
            "/api/v1/candles",
            private=False,
            params={
                "symbol": symbol,
                "type": interval,
                "startAt": start_at.isoformat(),
                "endAt": end_at.isoformat(),
            },
            retry=True,
        )
        rows = body.get("candles")
        if not isinstance(rows, list):
            raise XRocketError("candles response has no candles array")
        return rows

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
            self._owns_session = True
        return self._session

    async def _request(
        self,
        method: str,
        path: str,
        *,
        private: bool,
        params: Mapping[str, str | int] | list[tuple[str, str]] | None = None,
        json_body: dict[str, object] | None = None,
        retry: bool,
        order_create: bool = False,
    ) -> dict[str, object]:
        attempts = 1 if order_create or not retry else 1 + self._max_retries
        last_error: XRocketError | None = None
        for attempt in range(attempts):
            await self._limiter.acquire()
            try:
                status, body = await self._perform(
                    method,
                    path,
                    private=private,
                    params=params,
                    json_body=json_body,
                    order_create=order_create,
                )
            except UnknownOrderResultError:
                raise
            except _TRANSPORT as exc:
                if order_create:
                    raise UnknownOrderResultError(
                        "create order result is unknown after a timeout or connection loss; "
                        "the POST is not retried"
                    ) from exc
                last_error = XRocketError(f"transport error on {method} {path}")
                if attempt + 1 >= attempts:
                    raise last_error from exc
                await self._sleep(self._retry_delay(attempt))
                continue

            if status == 429:
                error = self._mapped(status)
                if order_create or not isinstance(error, RateLimitError) or attempt + 1 >= attempts:
                    raise error
                delay = error.retry_after_seconds
                if delay is None:
                    delay = self._retry_delay(attempt)
                self._log.warning("rate limited on %s %s", method, path)
                await self._sleep(delay)
                last_error = error
                continue

            if status >= 500:
                if order_create:
                    raise UnknownOrderResultError(
                        "create order result is unknown after an HTTP 5xx; the POST is not retried"
                    )
                error = self._mapped(status)
                last_error = error if isinstance(error, XRocketServerError) else XRocketServerError(f"HTTP {status}")
                if attempt + 1 >= attempts:
                    raise last_error
                await self._sleep(self._retry_delay(attempt))
                continue

            if status >= 400:
                raise self._mapped(status)

            if not isinstance(body, dict):
                if order_create:
                    raise UnknownOrderResultError(
                        "create order result is unknown because the response was not an object; "
                        "the POST is not retried"
                    )
                raise XRocketError(f"unexpected payload on {method} {path}")
            self._log.info("request %s %s -> %s", method, path, status)
            return body

        raise last_error or XRocketError(f"request failed {method} {path}")

    async def _perform(
        self,
        method: str,
        path: str,
        *,
        private: bool,
        params: Mapping[str, str | int] | list[tuple[str, str]] | None,
        json_body: dict[str, object] | None,
        order_create: bool,
    ) -> tuple[int, object]:
        if private:
            if self._token is None:
                raise ValueError("private requests require an API token")
            headers = build_private_headers(self._token)
        else:
            headers = build_public_headers()
        if json_body is not None:
            headers = {**headers, "Content-Type": "application/json"}
        session = await self._ensure_session()
        url = f"{self._base_url}{path}"
        async with session.request(method, url, headers=headers, params=params, json=json_body) as response:
            status = response.status
            raw = await response.read()
            response_headers = {key: value for key, value in response.headers.items()}
        body = _decode_body(raw, order_create=order_create)
        if status >= 400:
            self._last_mapped = map_http_error(status, body, response_headers)
        self._log.info("response %s %s status %s", method, path, status)
        return status, body

    def _mapped(self, status: int) -> XRocketError:
        if self._last_mapped is None:
            return XRocketError(f"HTTP {status}", status_code=status)
        return self._last_mapped

    def _retry_delay(self, attempt: int) -> float:
        return min(5.0, 0.2 * (2**attempt))


def _decode_body(raw: bytes, *, order_create: bool) -> object:
    if raw == b"":
        if order_create:
            raise UnknownOrderResultError(
                "create order result is unknown because the body was empty; the POST is not retried"
            )
        return {}
    try:
        return json.loads(raw)
    except ValueError as exc:
        if order_create:
            raise UnknownOrderResultError(
                "create order result is unknown because the body was not JSON; the POST is not retried"
            ) from exc
        raise XRocketError("response was not JSON") from exc


def _list_field(body: dict[str, object], name: str) -> list[object]:
    value = body.get(name)
    if not isinstance(value, list):
        raise XRocketError(f"response has no {name} array")
    return value


def page_limit_seconds(reported: int, span_seconds: float) -> int:
    """Page size in seconds for a window the exchange already rejected.

    ``maxIntervalInSeconds`` is documented as seconds. Testnet has returned
    ``259200000`` for a cap of 3 days, which is 3 days in milliseconds. When the
    raw number does not shrink the rejected window and it is divisible by 1000,
    the client treats it as milliseconds.
    """
    if reported <= 0:
        raise ValueError("max interval must be positive")
    if reported < span_seconds:
        return reported
    if reported % 1000 == 0:
        scaled = reported // 1000
        if 0 < scaled < span_seconds:
            return scaled
    raise ValueError("max interval does not shrink the window")


def _store_candles(collected: dict[datetime, Candle], rows: list[object]) -> None:
    for row in rows:
        if not isinstance(row, list):
            raise XRocketError("candle row must be an array")
        candle = Candle.from_array(row)
        collected[candle.start] = candle


def _repeated(name: str, values: list[str] | None) -> list[tuple[str, str]] | None:
    if not values:
        return None
    return [(name, value) for value in values]


def _order_lookup(order_id: str | None, client_order_id: str | None) -> dict[str, str]:
    """When both ids are present the API uses orderId, so only orderId is sent."""
    if order_id:
        return {"orderId": order_id}
    if client_order_id:
        validate_client_order_id(client_order_id)
        return {"clientOrderId": client_order_id}
    raise ValueError("orderId or clientOrderId is required")


def _stringify_order(payload: Mapping[str, object]) -> dict[str, object]:
    prepared: dict[str, object] = {}
    for key, value in payload.items():
        if value is None:
            continue
        if key in _MONEY_FIELDS:
            prepared[key] = format_decimal(Decimal(str(value)))
        else:
            prepared[key] = value
    return prepared
