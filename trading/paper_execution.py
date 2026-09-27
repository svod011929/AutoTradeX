"""Simulated IOC fills from the current full order book.

Fees and an extra slippage fraction are applied. Nothing is posted to the exchange.
"""

from decimal import Decimal

from trading.execution import Fill
from trading.market import BookSnapshot, walk_book
from xrocket.exceptions import UnknownOrderResultError


class PaperExecution:
    def __init__(self, *, fee_rate: Decimal, slippage_fraction: Decimal) -> None:
        self._fee_rate = fee_rate
        self._slippage = slippage_fraction
        self._orders: dict[str, Fill] = {}
        self.buy_calls = 0
        self.sell_calls = 0
        self.lose_next_buy_response = False

    async def market_buy(self, *, symbol: str, funds: Decimal, client_order_id: str, book: BookSnapshot) -> Fill:
        del symbol
        self.buy_calls += 1
        fill = self._buy_fill(funds, client_order_id, book)
        self._orders[client_order_id] = fill
        if self.lose_next_buy_response:
            self.lose_next_buy_response = False
            raise UnknownOrderResultError("paper buy response lost; the fill is stored for reconciliation")
        return fill

    async def market_sell(self, *, symbol: str, size: Decimal, client_order_id: str, book: BookSnapshot) -> Fill:
        del symbol
        self.sell_calls += 1
        fill = self._sell_fill(size, client_order_id, book)
        self._orders[client_order_id] = fill
        return fill

    async def get_order_by_client_id(self, client_order_id: str) -> Fill | None:
        return self._orders.get(client_order_id)

    def _buy_fill(self, funds: Decimal, client_order_id: str, book: BookSnapshot) -> Fill:
        ask = book.best_ask
        if ask is None or not book.is_full_snapshot:
            raise ValueError("paper buy needs a full ask snapshot")
        estimate = funds / (ask.price * (Decimal("1") + self._fee_rate))
        vwap = walk_book(book.asks, estimate) or ask.price
        price = vwap * (Decimal("1") + self._slippage)
        fee = funds * self._fee_rate / (Decimal("1") + self._fee_rate)
        base = (funds - fee) / price
        if base <= 0:
            raise ValueError("paper buy size is zero")
        return Fill(
            client_order_id=client_order_id,
            exchange_order_id=f"paper-{client_order_id}",
            status="completed",
            filled_quantity=base,
            average_price=price,
            fee=fee,
            fee_asset="USDT",
            funds=funds,
            size=base,
        )

    def _sell_fill(self, size: Decimal, client_order_id: str, book: BookSnapshot) -> Fill:
        bid = book.best_bid
        if bid is None or not book.is_full_snapshot:
            raise ValueError("paper sell needs a full bid snapshot")
        vwap = walk_book(book.bids, size) or bid.price
        price = vwap * (Decimal("1") - self._slippage)
        quote = size * price
        fee = quote * self._fee_rate
        return Fill(
            client_order_id=client_order_id,
            exchange_order_id=f"paper-{client_order_id}",
            status="completed",
            filled_quantity=size,
            average_price=price,
            fee=fee,
            fee_asset="USDT",
            funds=None,
            size=size,
        )
