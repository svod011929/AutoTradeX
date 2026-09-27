"""Sync local orders with the execution lookup.

New entries stay blocked until a pass finishes. A lost create is found by
clientOrderId. The create is not sent again.

When an exchange account view is attached, the same pass also reads the REST
trading balance, active orders, and the latest order history. Paper mode
leaves that view empty and only resolves the local fill map.
"""

from decimal import Decimal
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from database.models import Order, Position
from trading.execution import ExecutionInterface, Fill
from trading.order_manager import OrderManager

UNRESOLVED = ("unknown", "pending_submit")


class AccountSync(Protocol):
    async def trading_quote_available(self, asset: str = "USDT") -> Decimal | None:
        """REST trading balance for the quote asset."""

    async def list_active_orders(self) -> list[Fill]:
        """Working orders currently on the exchange."""

    async def list_recent_orders(self) -> list[Fill]:
        """Latest order history page."""


class ReconciliationService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        execution: ExecutionInterface,
        orders: OrderManager,
        *,
        account: AccountSync | None = None,
    ) -> None:
        self._factory = session_factory
        self._execution = execution
        self._orders = orders
        self._account = account
        self.blocking = False
        self.passes = 0
        self.last_quote: Decimal | None = None
        self.active_client_ids: list[str] = []
        self.history_client_ids: list[str] = []
        self.open_position_ids: list[int] = []

    def block(self) -> None:
        self.blocking = True

    async def on_unknown(self, client_order_id: str) -> None:
        del client_order_id
        self.blocking = True

    async def reconcile(self) -> list[str]:
        """Look up unresolved orders. Returns the client ids that were found."""
        self.blocking = True
        found: list[str] = []
        try:
            async with self._factory() as session:
                rows = list((await session.scalars(select(Order).where(Order.status.in_(UNRESOLVED)))).all())
                client_ids = [row.client_order_id for row in rows]
            for client_order_id in client_ids:
                fill = await self._execution.get_order_by_client_id(client_order_id)
                if fill is None:
                    continue
                await self._orders.apply_reconciled_fill(client_order_id, fill)
                found.append(client_order_id)
            await self._load_positions()
            if self._account is not None:
                await self._sync_account()
            self.passes += 1
            return found
        finally:
            self.blocking = False

    async def _load_positions(self) -> None:
        async with self._factory() as session:
            rows = list((await session.scalars(select(Position).where(Position.status == "open"))).all())
            self.open_position_ids = [row.id for row in rows]

    async def _sync_account(self) -> None:
        account = self._account
        if account is None:
            return
        self.last_quote = await account.trading_quote_available("USDT")
        active = await account.list_active_orders()
        history = await account.list_recent_orders()
        self.active_client_ids = [item.client_order_id for item in active]
        self.history_client_ids = [item.client_order_id for item in history]
