"""Shared bars, books, and accounts for stage-4 tests."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from database.models import BotSettings, DailyStat, Position, Strategy, StrategySettings, User
from services.market_feed import MarketSnapshot
from trading.market import Bar, BookLevel, BookSnapshot, sort_book
from xrocket.models import Symbol

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_symbol(name: str = "TON-USDT") -> Symbol:
    return Symbol.model_validate(
        {
            "symbol": name,
            "baseAsset": name.split("-")[0],
            "quoteAsset": "USDT",
            "baseMinSize": "0.01",
            "quoteMinSize": "0.01",
            "baseMaxSize": "1000000",
            "quoteMaxSize": "1000000",
            "minPrice": "0.0001",
            "maxPrice": "1000000",
            "baseIncrement": "0.01",
            "priceIncrement": "0.0001",
            "enableTrading": True,
        }
    )


def book_around(price: Decimal, *, size: Decimal = Decimal("100000")) -> BookSnapshot:
    gap = price * Decimal("0.0002")
    if gap <= 0:
        gap = Decimal("0.0001")
    bid = price - gap
    return sort_book(
        [BookLevel(bid, size), BookLevel(bid - gap, size)],
        [BookLevel(price, size), BookLevel(price + gap, size)],
    )


def trending_bars() -> list[Bar]:
    """Slow rise with a pullback every fourth bar and a volume spike on the last bar."""
    price = Decimal("100")
    bars: list[Bar] = []
    for index in range(90):
        step = Decimal("-0.5") if index % 4 == 3 else Decimal("0.2")
        price += step
        volume = Decimal("20") if index == 89 else Decimal("1")
        bars.append(
            Bar(
                open_time=START + timedelta(minutes=15 * index),
                open=price,
                high=price + Decimal("0.4"),
                low=price - Decimal("0.4"),
                close=price,
                volume=volume,
            )
        )
    return bars


def closed_moment(bars: list[Bar], *, grace_seconds: int = 5) -> datetime:
    return bars[-1].open_time + timedelta(minutes=15, seconds=grace_seconds)


def snapshot_for(symbol: str, bars: list[Bar], now: datetime, *, price: Decimal | None = None) -> MarketSnapshot:
    mark = price if price is not None else bars[-1].close
    return MarketSnapshot(
        symbol=symbol,
        bars=list(bars),
        book=book_around(mark),
        rules=make_symbol(symbol),
        api_ok=True,
        stale=False,
        fetched_at=now,
    )


async def make_account(
    factory: async_sessionmaker[AsyncSession],
    *,
    symbols: str = "TON-USDT",
    paused: bool = False,
    emergency_stop: bool = False,
    cash: Decimal = Decimal("1000"),
) -> int:
    async with factory() as session:
        user = User(telegram_id=1, username="daniel")
        session.add(user)
        await session.flush()
        session.add(
            BotSettings(
                user_id=user.id,
                execution_mode="paper",
                risk_profile="medium",
                enabled_symbols=symbols,
                timeframe="15m",
                is_paused=paused,
                emergency_stop=emergency_stop,
                paper_cash=cash,
            )
        )
        strategy = Strategy(
            user_id=user.id,
            name="Trend Following",
            kind="trend_following",
            timeframe="15m",
            is_enabled=True,
        )
        session.add(strategy)
        await session.flush()
        session.add(
            StrategySettings(
                strategy_id=strategy.id,
                cooldown_minutes=30,
            )
        )
        await session.commit()
        return user.id


async def add_position(
    factory: async_sessionmaker[AsyncSession],
    *,
    user_id: int,
    symbol: str,
    entry: Decimal,
    quantity: Decimal,
    stop: Decimal,
    take_profit: Decimal,
    peak: Decimal | None = None,
) -> int:
    async with factory() as session:
        position = Position(
            user_id=user_id,
            symbol=symbol,
            side="long",
            status="open",
            entry_price=entry,
            quantity=quantity,
            stop_loss=stop,
            take_profit=take_profit,
            trailing_stop=None,
            peak_price=peak if peak is not None else entry,
            opened_at=START,
        )
        session.add(position)
        await session.commit()
        return position.id


async def add_daily_loss(
    factory: async_sessionmaker[AsyncSession],
    *,
    user_id: int,
    day: datetime,
    net: Decimal,
) -> None:
    async with factory() as session:
        session.add(
            DailyStat(
                user_id=user_id,
                date=day.date(),
                trades_count=1,
                wins=0,
                losses=1,
                gross_pnl=net,
                fees=Decimal("0"),
                net_pnl=net,
            )
        )
        await session.commit()
