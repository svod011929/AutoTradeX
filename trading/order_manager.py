"""The only place an order intent is created.

The row, including clientOrderId, is committed before the execution call.
A lost response is marked unknown and is never sent again.
"""

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.trading_policy import MARKET_ENTRY_FIELD, MARKET_EXIT_FIELD, MARKET_TIME_IN_FORCE, generate_client_order_id
from database.models import Order
from database.retry import run_with_sqlite_lock_retry
from trading.execution import ExecutionInterface, Fill
from trading.market import BookSnapshot
from xrocket.exceptions import UnknownOrderResultError

OPEN_ORDER_STATUSES = ("pending_submit", "unknown", "working", "sending", "pending")


class OrderManager:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        execution: ExecutionInterface,
        *,
        on_unknown: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._factory = session_factory
        self._execution = execution
        self._on_unknown = on_unknown

    async def submit_market_buy(
        self,
        *,
        user_id: int,
        symbol: str,
        funds: Decimal,
        book: BookSnapshot,
        position_id: int | None = None,
    ) -> Order:
        if funds <= 0:
            raise ValueError("buy funds must be positive")
        return await self._submit(
            user_id=user_id,
            symbol=symbol,
            side="buy",
            funds=funds,
            size=None,
            book=book,
            position_id=position_id,
        )

    async def submit_market_sell(
        self,
        *,
        user_id: int,
        symbol: str,
        size: Decimal,
        book: BookSnapshot,
        position_id: int | None = None,
    ) -> Order:
        if size <= 0:
            raise ValueError("sell size must be positive")
        return await self._submit(
            user_id=user_id,
            symbol=symbol,
            side="sell",
            funds=None,
            size=size,
            book=book,
            position_id=position_id,
        )

    async def get_by_client_id(self, client_order_id: str) -> Order | None:
        async with self._factory() as session:
            return await session.scalar(select(Order).where(Order.client_order_id == client_order_id))

    async def _submit(
        self,
        *,
        user_id: int,
        symbol: str,
        side: str,
        funds: Decimal | None,
        size: Decimal | None,
        book: BookSnapshot,
        position_id: int | None,
    ) -> Order:
        blocking = await self._blocking_order(user_id, symbol, side)
        if blocking is not None:
            return blocking
        client_order_id = generate_client_order_id()
        order_id = await self._insert_intent(
            user_id=user_id,
            symbol=symbol,
            side=side,
            funds=funds,
            size=size,
            client_order_id=client_order_id,
            position_id=position_id,
        )
        try:
            if side == "buy":
                if funds is None:
                    raise ValueError("market buy uses funds")
                fill = await self._execution.market_buy(
                    symbol=symbol, funds=funds, client_order_id=client_order_id, book=book
                )
            elif side == "sell":
                if size is None:
                    raise ValueError("market sell uses size")
                fill = await self._execution.market_sell(
                    symbol=symbol, size=size, client_order_id=client_order_id, book=book
                )
            else:
                raise ValueError("side must be buy or sell")
        except UnknownOrderResultError:
            await self._mark_unknown(order_id)
            if self._on_unknown is not None:
                await self._on_unknown(client_order_id)
            return await self._require(order_id)
        await self._apply_fill(order_id, fill)
        return await self._require(order_id)

    async def sell_for_close(
        self,
        *,
        user_id: int,
        symbol: str,
        size: Decimal,
        book: BookSnapshot,
        position_id: int | None,
    ) -> tuple[Order, Fill | None]:
        """Commit the sell intent, send once, and leave a successful fill for the close transaction."""
        blocking = await self._blocking_order(user_id, symbol, "sell")
        if blocking is not None:
            return blocking, None
        client_order_id = generate_client_order_id()
        order_id = await self._insert_intent(
            user_id=user_id,
            symbol=symbol,
            side="sell",
            funds=None,
            size=size,
            client_order_id=client_order_id,
            position_id=position_id,
        )
        try:
            fill = await self._execution.market_sell(
                symbol=symbol, size=size, client_order_id=client_order_id, book=book
            )
        except UnknownOrderResultError:
            await self._mark_unknown(order_id)
            if self._on_unknown is not None:
                await self._on_unknown(client_order_id)
            return await self._require(order_id), None
        return await self._require(order_id), fill

    async def _blocking_order(self, user_id: int, symbol: str, side: str) -> Order | None:
        async with self._factory() as session:
            return await session.scalar(
                select(Order).where(
                    Order.user_id == user_id,
                    Order.symbol == symbol,
                    Order.side == side,
                    Order.status.in_(OPEN_ORDER_STATUSES),
                )
            )

    async def _insert_intent(
        self,
        *,
        user_id: int,
        symbol: str,
        side: str,
        funds: Decimal | None,
        size: Decimal | None,
        client_order_id: str,
        position_id: int | None,
    ) -> int:
        async def _op() -> int:
            async with self._factory() as session:
                try:
                    existing = await session.scalar(select(Order).where(Order.client_order_id == client_order_id))
                    if existing is not None:
                        return existing.id
                    order = Order(
                        user_id=user_id,
                        position_id=position_id,
                        client_order_id=client_order_id,
                        xrocket_order_id=None,
                        symbol=symbol,
                        side=side,
                        order_type="market",
                        status="pending_submit",
                        price=None,
                        quantity=size if side == "sell" else None,
                        filled_quantity=Decimal("0"),
                        average_price=None,
                        fee=None,
                        fee_asset=None,
                        time_in_force=MARKET_TIME_IN_FORCE,
                    )
                    if side == "buy":
                        order.price = None
                        order.quantity = funds
                    session.add(order)
                    await session.commit()
                    return order.id
                except Exception:
                    await session.rollback()
                    raise

        return await run_with_sqlite_lock_retry(_op)

    async def _mark_unknown(self, order_id: int) -> None:
        async def _op() -> None:
            async with self._factory() as session:
                order = await session.get(Order, order_id)
                if order is None:
                    return
                order.status = "unknown"
                order.updated_at = datetime.now(timezone.utc)
                await session.commit()

        await run_with_sqlite_lock_retry(_op)

    async def _apply_fill(self, order_id: int, fill: Fill) -> None:
        async def _op() -> None:
            async with self._factory() as session:
                order = await session.get(Order, order_id)
                if order is None:
                    return
                order.status = fill.status
                order.xrocket_order_id = fill.exchange_order_id
                order.filled_quantity = fill.filled_quantity
                order.average_price = fill.average_price
                order.fee = fill.fee
                order.fee_asset = fill.fee_asset
                order.updated_at = datetime.now(timezone.utc)
                await session.commit()

        await run_with_sqlite_lock_retry(_op)

    async def _require(self, order_id: int) -> Order:
        async with self._factory() as session:
            order = await session.get(Order, order_id)
            if order is None:
                raise RuntimeError(f"order {order_id} disappeared")
            return order

    async def apply_reconciled_fill(self, client_order_id: str, fill: Fill) -> Order | None:
        async with self._factory() as session:
            order = await session.scalar(select(Order).where(Order.client_order_id == client_order_id))
            if order is None:
                return None
            order.status = fill.status
            order.xrocket_order_id = fill.exchange_order_id
            order.filled_quantity = fill.filled_quantity
            order.average_price = fill.average_price
            order.fee = fill.fee
            order.fee_asset = fill.fee_asset
            order.updated_at = datetime.now(timezone.utc)
            await session.commit()
            return order


def order_field_for_side(side: str) -> str:
    if side == "buy":
        return MARKET_ENTRY_FIELD
    if side == "sell":
        return MARKET_EXIT_FIELD
    raise ValueError("side must be buy or sell")
