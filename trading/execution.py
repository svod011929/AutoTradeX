"""The only two ways the engine talks to a fill source."""

from decimal import Decimal
from typing import Protocol

from trading.market import BookSnapshot


class Fill:
    def __init__(
        self,
        *,
        client_order_id: str,
        exchange_order_id: str | None,
        status: str,
        filled_quantity: Decimal,
        average_price: Decimal,
        fee: Decimal,
        fee_asset: str,
        funds: Decimal | None = None,
        size: Decimal | None = None,
    ) -> None:
        self.client_order_id = client_order_id
        self.exchange_order_id = exchange_order_id
        self.status = status
        self.filled_quantity = filled_quantity
        self.average_price = average_price
        self.fee = fee
        self.fee_asset = fee_asset
        self.funds = funds
        self.size = size


class ExecutionInterface(Protocol):
    async def market_buy(self, *, symbol: str, funds: Decimal, client_order_id: str, book: BookSnapshot) -> Fill:
        """Spend quote funds. The caller already stored client_order_id."""

    async def market_sell(self, *, symbol: str, size: Decimal, client_order_id: str, book: BookSnapshot) -> Fill:
        """Sell an exact base size. Funds are not sent."""

    async def get_order_by_client_id(self, client_order_id: str) -> Fill | None:
        """Lookup after a lost response. This is not a second create."""
