"""Open longs, trail stops, and close in one database transaction.

The sell intent is stored before the execution call. The position, trade,
daily stats, and notification row commit together. A notifier runs only after
that commit. Telegram is not called from inside the transaction.
"""

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from database.models import DailyStat, Notification, Order, Position, Trade
from database.retry import run_with_sqlite_lock_retry
from trading.execution import Fill
from trading.market import BookSnapshot
from trading.order_manager import OrderManager
from xrocket.exceptions import PrecisionError
from xrocket.models import Symbol
from xrocket.precision import floor_size

AfterCommit = Callable[[int], Awaitable[None]]


class PositionManager:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        orders: OrderManager,
        *,
        after_commit: AfterCommit | None = None,
    ) -> None:
        self._factory = session_factory
        self._orders = orders
        self._after_commit = after_commit

    async def open_long(
        self,
        *,
        user_id: int,
        symbol: str,
        strategy_id: int | None,
        quantity: Decimal,
        entry_price: Decimal,
        stop: Decimal,
        take_profit: Decimal,
        buy_order_id: int,
        opened_at: datetime | None = None,
    ) -> Position:
        moment = opened_at or datetime.now(timezone.utc)

        async def _op() -> int:
            async with self._factory() as session:
                try:
                    position = Position(
                        user_id=user_id,
                        strategy_id=strategy_id,
                        symbol=symbol,
                        side="long",
                        status="open",
                        entry_price=entry_price,
                        quantity=quantity,
                        stop_loss=stop,
                        take_profit=take_profit,
                        trailing_stop=None,
                        peak_price=entry_price,
                        opened_at=moment,
                        closed_at=None,
                    )
                    session.add(position)
                    await session.flush()
                    buy = await session.get(Order, buy_order_id)
                    if buy is not None:
                        buy.position_id = position.id
                    note = Notification(
                        user_id=user_id,
                        event_type="buy",
                        payload_json=f'{{"symbol":"{symbol}","position_id":{position.id}}}',
                        sent=False,
                        status="pending",
                        attempts=0,
                    )
                    session.add(note)
                    await session.commit()
                    return note.id
                except Exception:
                    await session.rollback()
                    raise

        note_id = await run_with_sqlite_lock_retry(_op)
        await self._notify(note_id)
        async with self._factory() as session:
            position = await session.scalar(
                select(Position).where(
                    Position.user_id == user_id,
                    Position.symbol == symbol,
                    Position.status == "open",
                )
            )
            if position is None:
                raise RuntimeError("open position was not stored")
            return position

    async def open_positions(self, user_id: int) -> list[Position]:
        async with self._factory() as session:
            rows = await session.scalars(
                select(Position).where(Position.user_id == user_id, Position.status == "open")
            )
            return list(rows.all())

    async def protect(
        self,
        *,
        user_id: int,
        symbol: str,
        price: Decimal,
        atr: Decimal,
        book: BookSnapshot,
        symbol_rules: Symbol,
        activation_atr: Decimal,
        trail_atr: Decimal,
    ) -> list[str]:
        """Move the trail forward and close on stop, trail, or take profit."""
        events: list[str] = []
        async with self._factory() as session:
            rows = list(
                (
                    await session.scalars(
                        select(Position).where(
                            Position.user_id == user_id,
                            Position.symbol == symbol,
                            Position.status == "open",
                        )
                    )
                ).all()
            )
            for position in rows:
                self._advance_trail(position, price, atr, activation_atr, trail_atr)
            await session.commit()
        refreshed = await self.open_positions(user_id)
        for position in refreshed:
            if position.symbol != symbol:
                continue
            reason = self._exit_reason(position, price)
            if reason is None:
                continue
            closed = await self.close_long(
                position_id=position.id,
                price=price,
                book=book,
                symbol_rules=symbol_rules,
                reason=reason,
            )
            if closed:
                events.append(reason)
        return events

    async def close_long(
        self,
        *,
        position_id: int,
        price: Decimal,
        book: BookSnapshot,
        symbol_rules: Symbol,
        reason: str,
    ) -> bool:
        async with self._factory() as session:
            position = await session.get(Position, position_id)
            if position is None or position.status != "open" or position.quantity is None:
                return False
            user_id = position.user_id
            symbol = position.symbol
            quantity = position.quantity
            entry = position.entry_price or price
            opened_at = position.opened_at
        try:
            size = floor_size(quantity, symbol_rules)
        except PrecisionError:
            size = quantity
        order, fill = await self._orders.sell_for_close(
            user_id=user_id,
            symbol=symbol,
            size=size,
            book=book,
            position_id=position_id,
        )
        if fill is None or order.status == "unknown":
            return False
        note_id = await self._commit_close(
            position_id=position_id,
            order_id=order.id,
            fill=fill,
            entry=entry,
            opened_at=opened_at,
            reason=reason,
            user_id=user_id,
            symbol=symbol,
        )
        await self._notify(note_id)
        return True

    async def resume_completed_exits(self) -> int:
        """Finish a close that was filled and then interrupted before the commit."""
        async with self._factory() as session:
            open_rows = list((await session.scalars(select(Position).where(Position.status == "open"))).all())
            pending: list[tuple[Position, Order]] = []
            for position in open_rows:
                sell = await session.scalar(
                    select(Order).where(
                        Order.position_id == position.id,
                        Order.side == "sell",
                        Order.status == "completed",
                    )
                )
                if sell is None or sell.average_price is None or sell.filled_quantity <= 0:
                    continue
                if position.entry_price is None:
                    continue
                pending.append((position, sell))
            saved = [
                (
                    position.id,
                    sell.id,
                    position.entry_price,
                    position.opened_at,
                    position.user_id,
                    position.symbol,
                    Fill(
                        client_order_id=sell.client_order_id,
                        exchange_order_id=sell.xrocket_order_id,
                        status=sell.status,
                        filled_quantity=sell.filled_quantity,
                        average_price=sell.average_price,
                        fee=sell.fee or Decimal("0"),
                        fee_asset=sell.fee_asset or "USDT",
                        size=sell.filled_quantity,
                    ),
                )
                for position, sell in pending
                if position.entry_price is not None and sell.average_price is not None
            ]
        finished = 0
        for position_id, order_id, entry, opened_at, user_id, symbol, fill in saved:
            note_id = await self._commit_close(
                position_id=position_id,
                order_id=order_id,
                fill=fill,
                entry=entry,
                opened_at=opened_at,
                reason="exit",
                user_id=user_id,
                symbol=symbol,
            )
            await self._notify(note_id)
            finished += 1
        return finished

    def _advance_trail(
        self,
        position: Position,
        price: Decimal,
        atr: Decimal,
        activation_atr: Decimal,
        trail_atr: Decimal,
    ) -> None:
        if position.entry_price is None or atr <= 0:
            return
        peak = position.peak_price if position.peak_price is not None else position.entry_price
        if price > peak:
            peak = price
        position.peak_price = peak
        if peak < position.entry_price + (atr * activation_atr):
            return
        candidate = peak - (atr * trail_atr)
        if position.trailing_stop is None or candidate > position.trailing_stop:
            position.trailing_stop = candidate

    def _exit_reason(self, position: Position, price: Decimal) -> str | None:
        stop = position.stop_loss
        if position.trailing_stop is not None and (stop is None or position.trailing_stop > stop):
            stop = position.trailing_stop
            if price <= stop:
                return "trailing"
        elif stop is not None and price <= stop:
            return "sl"
        if position.take_profit is not None and price >= position.take_profit:
            return "tp"
        return None

    async def _commit_close(
        self,
        *,
        position_id: int,
        order_id: int,
        fill: Fill,
        entry: Decimal,
        opened_at: datetime | None,
        reason: str,
        user_id: int,
        symbol: str,
    ) -> int:
        async def _op() -> int:
            async with self._factory() as session:
                try:
                    position = await session.get(Position, position_id)
                    order = await session.get(Order, order_id)
                    if position is None or order is None:
                        raise RuntimeError("close target missing")
                    now = datetime.now(timezone.utc)
                    order.status = fill.status
                    order.xrocket_order_id = fill.exchange_order_id
                    order.filled_quantity = fill.filled_quantity
                    order.average_price = fill.average_price
                    order.fee = fill.fee
                    order.fee_asset = fill.fee_asset
                    order.position_id = position.id
                    order.updated_at = now
                    quantity = fill.filled_quantity
                    exit_price = fill.average_price
                    gross = (exit_price - entry) * quantity
                    entry_fee = await self._entry_fee(session, position.id)
                    fees = entry_fee + fill.fee
                    net = gross - fees
                    position.status = "closed"
                    position.closed_at = now
                    position.quantity = quantity
                    trade = Trade(
                        user_id=user_id,
                        position_id=position.id,
                        order_id=order.id,
                        symbol=symbol,
                        side="long",
                        quantity=quantity,
                        entry_price=entry,
                        exit_price=exit_price,
                        gross_pnl=gross,
                        fees=fees,
                        net_pnl=net,
                        opened_at=opened_at,
                        closed_at=now,
                    )
                    session.add(trade)
                    await self._add_daily(session, user_id, now, gross, fees, net)
                    note = Notification(
                        user_id=user_id,
                        event_type=reason,
                        payload_json=f'{{"symbol":"{symbol}","position_id":{position.id}}}',
                        sent=False,
                        status="pending",
                        attempts=0,
                    )
                    session.add(note)
                    await session.commit()
                    return note.id
                except Exception:
                    await session.rollback()
                    raise

        return await run_with_sqlite_lock_retry(_op)

    async def _entry_fee(self, session: AsyncSession, position_id: int) -> Decimal:
        buy = await session.scalar(
            select(Order).where(Order.position_id == position_id, Order.side == "buy")
        )
        if buy is None or buy.fee is None:
            return Decimal("0")
        return buy.fee

    async def _add_daily(
        self,
        session: AsyncSession,
        user_id: int,
        moment: datetime,
        gross: Decimal,
        fees: Decimal,
        net: Decimal,
    ) -> None:
        day = moment.date()
        stats = await session.scalar(select(DailyStat).where(DailyStat.user_id == user_id, DailyStat.date == day))
        if stats is None:
            stats = DailyStat(
                user_id=user_id,
                date=day,
                trades_count=0,
                wins=0,
                losses=0,
                gross_pnl=Decimal("0"),
                fees=Decimal("0"),
                net_pnl=Decimal("0"),
            )
            session.add(stats)
        stats.trades_count += 1
        if net > 0:
            stats.wins += 1
        elif net < 0:
            stats.losses += 1
        stats.gross_pnl += gross
        stats.fees += fees
        stats.net_pnl += net

    async def _notify(self, note_id: int) -> None:
        if self._after_commit is None:
            return
        await self._after_commit(note_id)
