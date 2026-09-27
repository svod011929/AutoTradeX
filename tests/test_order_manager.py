"""The order row, including clientOrderId, is stored before the fill call."""

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from database.models import Order, User
from database.session import get_sessionmaker
from trading.market import BookSnapshot
from trading.order_manager import OrderManager, order_field_for_side
from trading.paper_execution import PaperExecution
from tests.market_data import book_around
from xrocket.exceptions import OrdersDisabledError
from xrocket.rest_client import XRocketRestClient
from trading.xrocket_execution import XRocketExecution
from core.config import Settings


@pytest.mark.asyncio
async def test_intent_is_committed_before_the_buy_call(settings) -> None:
    del settings
    factory = get_sessionmaker()
    async with factory() as session:
        session.add(User(telegram_id=7, username="daniel"))
        await session.commit()
        user_id = (await session.scalar(select(User))).id
    execution = _WatchingExecution(factory)
    orders = OrderManager(factory, execution)
    book = book_around(Decimal("2"))
    order = await orders.submit_market_buy(user_id=user_id, symbol="TON-USDT", funds=Decimal("20"), book=book)
    assert execution.seen_status == "pending_submit"
    assert order.status == "completed"
    assert order.client_order_id
    assert order.quantity == Decimal("20")
    assert order_field_for_side("buy") == "funds"
    assert order_field_for_side("sell") == "size"


@pytest.mark.asyncio
async def test_a_lost_buy_response_is_not_sent_again(settings) -> None:
    del settings
    factory = get_sessionmaker()
    async with factory() as session:
        session.add(User(telegram_id=8, username="daniel"))
        await session.commit()
        user_id = (await session.scalar(select(User))).id
    execution = PaperExecution(fee_rate=Decimal("0.01"), slippage_fraction=Decimal("0"))
    execution.lose_next_buy_response = True
    orders = OrderManager(factory, execution)
    book = book_around(Decimal("2"))
    first = await orders.submit_market_buy(user_id=user_id, symbol="TON-USDT", funds=Decimal("20"), book=book)
    second = await orders.submit_market_buy(user_id=user_id, symbol="TON-USDT", funds=Decimal("20"), book=book)
    assert first.client_order_id == second.client_order_id
    assert first.status == "unknown"
    assert execution.buy_calls == 1
    async with factory() as session:
        count = await session.scalar(select(func.count()).select_from(Order))
    assert count == 1


@pytest.mark.asyncio
async def test_sell_uses_base_size(settings) -> None:
    del settings
    factory = get_sessionmaker()
    async with factory() as session:
        session.add(User(telegram_id=9, username="daniel"))
        await session.commit()
        user_id = (await session.scalar(select(User))).id
    execution = PaperExecution(fee_rate=Decimal("0.01"), slippage_fraction=Decimal("0"))
    orders = OrderManager(factory, execution)
    order = await orders.submit_market_sell(
        user_id=user_id,
        symbol="TON-USDT",
        size=Decimal("1.25"),
        book=book_around(Decimal("2")),
    )
    assert order.quantity == Decimal("1.25")
    assert order.filled_quantity == Decimal("1.25")
    assert execution.sell_calls == 1
    stored = execution._orders[order.client_order_id]
    assert stored.funds is None
    assert stored.size == Decimal("1.25")


@pytest.mark.asyncio
async def test_exchange_execution_is_off_in_paper() -> None:
    settings = Settings(_env_file=None, execution_mode="paper", allow_exchange_orders=True)
    client = XRocketRestClient("https://exchange.example", token=None, allow_order_placement=False)
    execution = XRocketExecution(client, settings)
    with pytest.raises(OrdersDisabledError):
        await execution.market_sell(
            symbol="TON-USDT",
            size=Decimal("1"),
            client_order_id="abc",
            book=BookSnapshot((), (), False),
        )


class _WatchingExecution(PaperExecution):
    def __init__(self, factory) -> None:
        super().__init__(fee_rate=Decimal("0.01"), slippage_fraction=Decimal("0"))
        self._factory = factory
        self.seen_status: str | None = None

    async def market_buy(self, *, symbol: str, funds: Decimal, client_order_id: str, book: BookSnapshot):
        async with self._factory() as session:
            row = await session.scalar(select(Order).where(Order.client_order_id == client_order_id))
            self.seen_status = None if row is None else row.status
        return await super().market_buy(symbol=symbol, funds=funds, client_order_id=client_order_id, book=book)
