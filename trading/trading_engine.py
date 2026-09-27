"""One trading loop: market, indicators, strategy, signal, risk, order, position.

Pause, emergency stop, daily loss, and an unfinished reconciliation block new
buys. Open positions stay protected while prices are fresh. Stale data blocks
both new buys and protective sells.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import RiskName, Settings
from core.trading_policy import fee_rate_to_fraction
from database.models import BotSettings, DailyStat, Order, Position, Strategy, StrategySettings
from services.market_feed import MarketSnapshot, MarketSource, empty_snapshot
from services.reconciliation_service import ReconciliationService
from services.watchdog import Watchdog
from trading.indicators import atr
from trading.market import confirmed_closed_bars, ensure_aware
from trading.order_manager import OrderManager
from trading.position_manager import PositionManager
from trading.risk_manager import RiskInput, RiskManager
from trading.signal_engine import SignalEngine
from trading.strategy_engine import StrategyParams, evaluate

logger = logging.getLogger("autotrade.engine")


@dataclass(frozen=True)
class CycleReport:
    entries: tuple[str, ...]
    protections: tuple[str, ...]
    entry_blocks: tuple[str, ...]


@dataclass
class _Runtime:
    strategy_id: int | None
    params: StrategyParams
    profile: RiskName
    symbols: list[str]
    timeframe: str
    paused: bool
    emergency_stop: bool
    cooldown_minutes: int
    cash: Decimal
    activation_atr: Decimal
    trail_atr: Decimal


class TradingEngine:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        user_id: int,
        orders: OrderManager,
        positions: PositionManager,
        reconciliation: ReconciliationService,
        watchdog: Watchdog,
        feed: MarketSource | None = None,
        clock: Callable[[], datetime] | None = None,
        ws_healthy: Callable[[], bool] | None = None,
        reconcile_each_cycle: bool = True,
    ) -> None:
        self._factory = session_factory
        self._settings = settings
        self._user_id = user_id
        self._orders = orders
        self._positions = positions
        self._reconciliation = reconciliation
        self._watchdog = watchdog
        self._feed = feed
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._ws_healthy = ws_healthy
        self._reconcile_each_cycle = reconcile_each_cycle
        self._risk = RiskManager()
        self._signals = SignalEngine()
        self._snapshots: dict[str, MarketSnapshot] = {}
        self._seen: dict[str, MarketSnapshot] = {}
        self.entries_blocked = False
        self._stop = False
        self.last_report = CycleReport((), (), ())

    def push_snapshot(self, snapshot: MarketSnapshot) -> None:
        self._snapshots[snapshot.symbol] = snapshot

    def block_entries(self) -> None:
        self.entries_blocked = True

    def allow_entries(self) -> None:
        self.entries_blocked = False

    def stop(self) -> None:
        self._stop = True

    async def run(
        self,
        *,
        cycles: int | None = None,
        stop: asyncio.Event | None = None,
        interval_seconds: float = 20,
    ) -> int:
        completed = 0
        while not self._stop:
            if stop is not None and stop.is_set():
                break
            await self.run_cycle()
            completed += 1
            if cycles is not None and completed >= cycles:
                break
            if stop is None:
                await asyncio.sleep(interval_seconds)
                continue
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            except TimeoutError:
                continue
            else:
                break
        return completed

    async def run_cycle(self, *, now: datetime | None = None) -> CycleReport:
        moment = ensure_aware(now or self._clock())
        await self._watchdog.beat("trading")
        if self._reconcile_each_cycle:
            await self._reconciliation.reconcile()
        await self._positions.resume_completed_exits()
        runtime = await self._load_runtime(moment)
        if runtime is None:
            self.last_report = CycleReport((), (), ("setup",))
            return self.last_report
        entries: list[str] = []
        protections: list[str] = []
        blocks: list[str] = []
        snapshots: dict[str, MarketSnapshot] = {}
        for symbol in runtime.symbols:
            snapshots[symbol] = await self._snapshot(symbol, runtime.timeframe, moment)
        self._seen = snapshots
        for symbol in runtime.symbols:
            snapshot = snapshots[symbol]
            fresh = self._fresh(snapshot)
            bid = snapshot.book.best_bid
            if fresh and snapshot.rules is not None and bid is not None:
                protections.extend(
                    await self._positions.protect(
                        user_id=self._user_id,
                        symbol=symbol,
                        price=bid.price,
                        atr=self._atr(snapshot, runtime),
                        book=snapshot.book,
                        symbol_rules=snapshot.rules,
                        activation_atr=runtime.activation_atr,
                        trail_atr=runtime.trail_atr,
                    )
                )
                await self._adopt_buys(snapshot, runtime)
            if not self._entries_allowed(runtime):
                blocks.append(self._block_name(runtime))
                continue
            if not fresh or snapshot.rules is None or snapshot.book.best_ask is None:
                blocks.append("stale")
                continue
            closed = confirmed_closed_bars(
                snapshot.bars,
                timeframe=runtime.timeframe,
                now=moment,
                grace_seconds=self._settings.candle_close_grace_seconds,
            )
            view = evaluate(closed, runtime.params)
            if not isinstance(view.bar_open, datetime) or view.close is None:
                blocks.append("not_enough_closed_bars")
                continue
            async with self._factory() as session:
                stored = await self._signals.register(
                    session,
                    user_id=self._user_id,
                    strategy_id=runtime.strategy_id,
                    symbol=symbol,
                    timeframe=runtime.timeframe,
                    side="buy",
                    price=view.close,
                    open_time=view.bar_open,
                    valid=view.buy,
                )
            if not view.buy or stored.duplicate:
                blocks.append("duplicate" if stored.duplicate else ",".join(view.reasons))
                continue
            decision = await self._assess(runtime, snapshot, symbol, moment, signal_valid=True)
            if not decision.allowed or decision.funds is None or decision.stop is None or decision.take_profit is None:
                blocks.append(",".join(decision.reasons))
                continue
            order = await self._orders.submit_market_buy(
                user_id=self._user_id,
                symbol=symbol,
                funds=decision.funds,
                book=snapshot.book,
            )
            if order.status != "completed" or order.filled_quantity <= 0 or order.average_price is None:
                blocks.append(order.status)
                continue
            await self._positions.open_long(
                user_id=self._user_id,
                symbol=symbol,
                strategy_id=runtime.strategy_id,
                quantity=order.filled_quantity,
                entry_price=order.average_price,
                stop=decision.stop,
                take_profit=decision.take_profit,
                buy_order_id=order.id,
                opened_at=moment,
            )
            entries.append(symbol)
        await self._sync_cash(runtime)
        self.last_report = CycleReport(tuple(entries), tuple(protections), tuple(blocks))
        if blocks:
            logger.info("entries held: %s", ",".join(blocks))
        return self.last_report

    def _entries_allowed(self, runtime: _Runtime) -> bool:
        if self.entries_blocked or runtime.paused or runtime.emergency_stop:
            return False
        return not self._reconciliation.blocking

    def _block_name(self, runtime: _Runtime) -> str:
        if self.entries_blocked:
            return "shutdown"
        if runtime.emergency_stop:
            return "emergency_stop"
        if runtime.paused:
            return "paused"
        if self._reconciliation.blocking:
            return "reconciliation"
        return "blocked"

    def _position_mark(self, position: Position, fallback: Decimal) -> Decimal:
        seen = self._seen.get(position.symbol)
        if seen is not None and seen.book.best_bid is not None:
            return seen.book.best_bid.price
        if position.entry_price is not None:
            return position.entry_price
        return fallback

    def _fresh(self, snapshot: MarketSnapshot) -> bool:
        if not snapshot.api_ok or snapshot.stale or not snapshot.book.is_full_snapshot:
            return False
        if snapshot.book.best_bid is None or snapshot.book.best_ask is None:
            return False
        if self._ws_healthy is not None and not self._ws_healthy():
            return False
        return True

    async def _snapshot(self, symbol: str, timeframe: str, now: datetime) -> MarketSnapshot:
        injected = self._snapshots.get(symbol)
        if injected is not None:
            return injected
        if self._feed is None:
            return empty_snapshot(symbol, now)
        return await self._feed.load(symbol, timeframe, now)

    def _atr(self, snapshot: MarketSnapshot, runtime: _Runtime) -> Decimal:
        closed = confirmed_closed_bars(
            snapshot.bars,
            timeframe=runtime.timeframe,
            now=snapshot.fetched_at,
            grace_seconds=self._settings.candle_close_grace_seconds,
        )
        if len(closed) <= runtime.params.atr_period:
            return Decimal("0")
        values = atr(
            [bar.high for bar in closed],
            [bar.low for bar in closed],
            [bar.close for bar in closed],
            runtime.params.atr_period,
        )
        if not values:
            return Decimal("0")
        return values[-1]

    async def _adopt_buys(self, snapshot: MarketSnapshot, runtime: _Runtime) -> None:
        if snapshot.rules is None:
            return
        current_atr = self._atr(snapshot, runtime)
        if current_atr <= 0:
            return
        async with self._factory() as session:
            buys = list(
                (
                    await session.scalars(
                        select(Order).where(
                            Order.user_id == self._user_id,
                            Order.symbol == snapshot.symbol,
                            Order.side == "buy",
                            Order.status == "completed",
                            Order.position_id.is_(None),
                        )
                    )
                ).all()
            )
            saved = [
                (order.id, order.filled_quantity, order.average_price)
                for order in buys
                if order.average_price is not None and order.filled_quantity > 0
            ]
        for order_id, quantity, price in saved:
            if price is None:
                continue
            stop = price - (current_atr * runtime.params.atr_sl_multiplier)
            if stop <= 0 or stop >= price:
                continue
            take_profit = price + (price - stop) * runtime.params.take_profit_rr
            await self._positions.open_long(
                user_id=self._user_id,
                symbol=snapshot.symbol,
                strategy_id=runtime.strategy_id,
                quantity=quantity,
                entry_price=price,
                stop=stop,
                take_profit=take_profit,
                buy_order_id=order_id,
            )

    async def _assess(self, runtime: _Runtime, snapshot: MarketSnapshot, symbol: str, now: datetime, *, signal_valid: bool):
        assert snapshot.rules is not None
        assert snapshot.book.best_ask is not None
        positions = await self._positions.open_positions(self._user_id)
        on_symbol = any(item.symbol == symbol for item in positions)
        equity = runtime.cash
        for item in positions:
            if item.quantity is None:
                continue
            equity += item.quantity * self._position_mark(item, snapshot.book.best_ask.price)
        async with self._factory() as session:
            stats = await session.scalar(
                select(DailyStat).where(DailyStat.user_id == self._user_id, DailyStat.date == now.date())
            )
            latest = await session.scalar(
                select(Order.created_at)
                .where(Order.user_id == self._user_id, Order.symbol == symbol)
                .order_by(Order.created_at.desc())
            )
        daily = stats.net_pnl if stats is not None else Decimal("0")
        cooldown = False
        if latest is not None:
            cooldown = ensure_aware(now) - ensure_aware(latest) < timedelta(minutes=runtime.cooldown_minutes)
        fee = fee_rate_to_fraction(self._settings.default_fee_rate, self._settings.fee_rate_is_fraction)
        return self._risk.assess(
            RiskInput(
                equity=equity,
                available_quote=runtime.cash,
                profile=runtime.profile,
                fee_rate=fee,
                entry=snapshot.book.best_ask.price,
                atr=self._atr(snapshot, runtime),
                atr_sl_multiplier=runtime.params.atr_sl_multiplier,
                take_profit_rr=runtime.params.take_profit_rr,
                symbol=snapshot.rules,
                book=snapshot.book,
                open_positions=len(positions),
                has_position_on_symbol=on_symbol,
                daily_net_pnl=daily,
                cooldown_active=cooldown,
                duplicate_signal=False,
                signal_valid=signal_valid,
                api_ok=snapshot.api_ok,
                market_data_ok=True,
                data_stale=snapshot.stale,
                paused=runtime.paused,
                emergency_stop=runtime.emergency_stop,
                reconciliation_blocking=self._reconciliation.blocking,
                max_spread_fraction=self._settings.max_spread_fraction,
                max_slippage_fraction=self._settings.max_slippage_fraction,
                cash_reserve_fraction=self._settings.cash_reserve_fraction,
            )
        )

    async def _load_runtime(self, now: datetime) -> _Runtime | None:
        del now
        async with self._factory() as session:
            bot = await session.scalar(select(BotSettings).where(BotSettings.user_id == self._user_id))
            if bot is None:
                return None
            strategy = await session.scalar(select(Strategy).where(Strategy.user_id == self._user_id, Strategy.is_enabled.is_(True)))
            settings_row = None
            if strategy is not None:
                settings_row = await session.scalar(
                    select(StrategySettings).where(StrategySettings.strategy_id == strategy.id)
                )
            profile = _profile_name(bot.risk_profile)
            if profile is None:
                return None
            symbols = [part.strip() for part in bot.enabled_symbols.split(",") if part.strip()]
            params = _params(self._settings, settings_row)
            activation = (
                settings_row.trailing_activation_atr if settings_row is not None else self._settings.trailing_activation_atr
            )
            trail = settings_row.trailing_atr_multiplier if settings_row is not None else self._settings.trailing_atr_multiplier
            cash = await _paper_cash(session, self._user_id, self._settings.paper_starting_equity)
            return _Runtime(
                strategy_id=None if strategy is None else strategy.id,
                params=params,
                profile=profile,
                symbols=symbols,
                timeframe=bot.timeframe,
                paused=bot.is_paused,
                emergency_stop=bot.emergency_stop,
                cooldown_minutes=settings_row.cooldown_minutes if settings_row is not None else self._settings.cooldown_minutes,
                cash=cash,
                activation_atr=activation,
                trail_atr=trail,
            )

    async def _sync_cash(self, runtime: _Runtime) -> None:
        async with self._factory() as session:
            cash = await _paper_cash(session, self._user_id, self._settings.paper_starting_equity)
            bot = await session.scalar(select(BotSettings).where(BotSettings.user_id == self._user_id))
            if bot is None:
                return
            bot.paper_cash = cash
            await session.commit()
        runtime.cash = cash


def _profile_name(value: str) -> RiskName | None:
    match value:
        case "low":
            return "low"
        case "medium":
            return "medium"
        case "high":
            return "high"
        case _:
            return None


def _params(settings: Settings, row: StrategySettings | None) -> StrategyParams:
    if row is None:
        return StrategyParams(
            volume_ma_period=settings.volume_ma_period,
            min_volume_ratio=settings.min_volume_ratio,
            rsi_low=settings.rsi_entry_min,
            rsi_high=settings.rsi_entry_max,
            atr_sl_multiplier=settings.atr_sl_multiplier,
            take_profit_rr=settings.take_profit_rr,
        )
    return StrategyParams(
        ema_fast=row.ema_fast,
        ema_slow=row.ema_slow,
        rsi_period=row.rsi_period,
        atr_period=row.atr_period,
        volume_ma_period=settings.volume_ma_period,
        min_volume_ratio=row.min_volume_ratio,
        rsi_low=settings.rsi_entry_min,
        rsi_high=settings.rsi_entry_max,
        atr_sl_multiplier=row.atr_sl_multiplier,
        take_profit_rr=row.take_profit_rr,
    )


async def _paper_cash(session: AsyncSession, user_id: int, seed: Decimal) -> Decimal:
    """Replay completed paper fills onto the starting equity. Unknown orders are not counted."""
    orders = list((await session.scalars(select(Order).where(Order.user_id == user_id, Order.status == "completed"))).all())
    cash = seed
    for order in orders:
        if order.side == "buy" and order.quantity is not None:
            cash -= order.quantity
        elif order.side == "sell" and order.average_price is not None:
            fee = order.fee or Decimal("0")
            cash += (order.filled_quantity * order.average_price) - fee
    return cash
