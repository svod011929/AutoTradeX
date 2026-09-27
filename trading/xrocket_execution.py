"""Live orders through the REST client.

Creates nothing unless execution mode is testnet or mainnet and the manual
flag is on. Paper mode cannot reach POST /api/v1/orders from here.
"""

from decimal import Decimal

from core.config import Settings
from trading.execution import Fill
from trading.market import BookSnapshot
from xrocket.exceptions import OrderNotFoundError, OrdersDisabledError
from xrocket.models import ExchangeOrder
from xrocket.rest_client import XRocketRestClient


class XRocketExecution:
    def __init__(self, client: XRocketRestClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    @property
    def orders_enabled(self) -> bool:
        return self._settings.exchange_orders_allowed()

    async def market_buy(self, *, symbol: str, funds: Decimal, client_order_id: str, book: BookSnapshot) -> Fill:
        del book
        self._require_enabled()
        order = await self._client.create_market_entry(symbol, "buy", funds, client_order_id)
        return _fill_from_order(order)

    async def market_sell(self, *, symbol: str, size: Decimal, client_order_id: str, book: BookSnapshot) -> Fill:
        del book
        self._require_enabled()
        order = await self._client.create_market_exit(symbol, size, client_order_id)
        return _fill_from_order(order)

    async def get_order_by_client_id(self, client_order_id: str) -> Fill | None:
        try:
            order = await self._client.get_order(client_order_id=client_order_id)
        except OrderNotFoundError:
            return None
        return _fill_from_order(order)

    async def trading_quote_available(self, asset: str = "USDT") -> Decimal | None:
        """REST trading balance. Funding is not read and is not transferred."""
        balances = await self._client.get_trading_balances()
        for row in balances:
            if row.asset.upper() == asset.upper():
                return row.available
        return Decimal("0")

    async def list_active_orders(self) -> list[Fill]:
        return [_fill_from_order(order) for order in await self._client.get_active_orders()]

    async def list_recent_orders(self) -> list[Fill]:
        page = await self._client.get_order_history(page_size=100, current_page=1)
        return [_fill_from_order(order) for order in page.orders]

    def _require_enabled(self) -> None:
        if not self.orders_enabled:
            raise OrdersDisabledError("xRocket orders stay off until TESTNET or MAINNET is enabled manually")


def _fill_from_order(order: ExchangeOrder) -> Fill:
    price = order.average_price if order.average_price is not None else Decimal("0")
    return Fill(
        client_order_id=order.client_order_id or "",
        exchange_order_id=order.id,
        status=order.status,
        filled_quantity=order.filled_quantity,
        average_price=price,
        fee=order.fee,
        fee_asset=order.fee_asset,
        funds=order.funds,
        size=order.size,
    )
