"""A lost create is resolved by clientOrderId and is not sent again."""

from decimal import Decimal

import pytest
from sqlalchemy import select

from database.models import Order, User
from database.session import get_sessionmaker
from trading.order_manager import OrderManager
from trading.paper_execution import PaperExecution
from services.reconciliation_service import ReconciliationService
from tests.market_data import book_around


@pytest.mark.asyncio
async def test_reconciliation_finds_the_buy_and_blocks_while_it_runs(settings) -> None:
    del settings
    factory = get_sessionmaker()
    async with factory() as session:
        session.add(User(telegram_id=5, username="daniel"))
        await session.commit()
        user = await session.scalar(select(User))
        assert user is not None
        user_id = user.id
    execution = PaperExecution(fee_rate=Decimal("0.01"), slippage_fraction=Decimal("0"))
    execution.lose_next_buy_response = True
    holder: dict[str, ReconciliationService] = {}

    async def on_unknown(client_order_id: str) -> None:
        await holder["reconciliation"].on_unknown(client_order_id)

    orders = OrderManager(factory, execution, on_unknown=on_unknown)
    reconciliation = ReconciliationService(factory, execution, orders)
    holder["reconciliation"] = reconciliation
    await orders.submit_market_buy(
        user_id=user_id,
        symbol="TON-USDT",
        funds=Decimal("25"),
        book=book_around(Decimal("2")),
    )
    assert reconciliation.blocking is True
    assert execution.buy_calls == 1
    found = await reconciliation.reconcile()
    assert reconciliation.blocking is False
    assert len(found) == 1
    assert execution.buy_calls == 1
    async with factory() as session:
        order = await session.scalar(select(Order).where(Order.client_order_id == found[0]))
    assert order is not None
    assert order.status == "completed"
    assert order.filled_quantity > 0
