"""Persist one signal per closed bar. A repeat of the same bar is a duplicate."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import TradeSignal
from database.retry import run_with_sqlite_lock_retry

ACCEPTED = "accepted"
DUPLICATE = "duplicate"


def bar_reason(open_time: datetime) -> str:
    return f"bar:{open_time.isoformat()}"


@dataclass(frozen=True)
class StoredSignal:
    signal: TradeSignal
    created: bool

    @property
    def duplicate(self) -> bool:
        return not self.created


class SignalEngine:
    async def register(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        strategy_id: int | None,
        symbol: str,
        timeframe: str,
        side: str,
        price: Decimal | None,
        open_time: datetime,
        valid: bool,
    ) -> StoredSignal:
        reason = bar_reason(open_time)

        async def _write() -> StoredSignal:
            try:
                existing = await session.scalar(
                    select(TradeSignal).where(
                        TradeSignal.user_id == user_id,
                        TradeSignal.symbol == symbol,
                        TradeSignal.timeframe == timeframe,
                        TradeSignal.side == side,
                        TradeSignal.reason == reason,
                    )
                )
                if existing is not None:
                    return StoredSignal(existing, created=False)
                signal = TradeSignal(
                    user_id=user_id,
                    strategy_id=strategy_id,
                    symbol=symbol,
                    timeframe=timeframe,
                    side=side,
                    price=price,
                    status=ACCEPTED if valid else "rejected",
                    reason=reason,
                )
                session.add(signal)
                await session.commit()
                return StoredSignal(signal, created=True)
            except Exception:
                await session.rollback()
                raise

        return await run_with_sqlite_lock_retry(_write)
