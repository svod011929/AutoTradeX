"""Replay the live strategy and risk manager on closed bars.

A decision uses only bars that have already closed. The fill is the next bar's
open, adjusted by the spread and slippage. That bar's close is not an entry price.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from core.config import Settings
from core.trading_policy import MAX_SPREAD_FRACTION, PAPER_SLIPPAGE_FRACTION
from trading.market import Bar, BookLevel, BookSnapshot, sort_book
from trading.risk_manager import RiskInput, RiskManager
from trading.strategy_engine import StrategyParams, evaluate
from xrocket.models import Symbol

from .metrics import Performance, TradePoint, equity_drawdown, summarize


@dataclass(frozen=True)
class BacktestFill:
    time: datetime
    side: str
    price: Decimal
    decision_open_time: datetime | None


@dataclass(frozen=True)
class BacktestTrade:
    symbol: str
    entry_time: datetime
    exit_time: datetime
    entry_price: Decimal
    exit_price: Decimal
    quantity: Decimal
    stop: Decimal
    gross_pnl: Decimal
    fees: Decimal
    net_pnl: Decimal
    r_multiple: Decimal | None
    reason: str


@dataclass(frozen=True)
class BacktestResult:
    symbol: str
    starting_equity: Decimal
    ending_equity: Decimal
    trades: tuple[BacktestTrade, ...]
    fills: tuple[BacktestFill, ...]
    equity_curve: tuple[tuple[datetime, Decimal], ...]
    performance: Performance
    max_drawdown: Decimal
    current_drawdown: Decimal


@dataclass
class _Open:
    quantity: Decimal
    entry: Decimal
    stop: Decimal
    take_profit: Decimal
    entry_fee: Decimal
    opened_at: datetime
    peak: Decimal
    trail: Decimal | None
    atr: Decimal
    decision_open: datetime


def buy_fill_price(
    next_open: Decimal,
    *,
    spread_fraction: Decimal = MAX_SPREAD_FRACTION,
    slippage_fraction: Decimal = PAPER_SLIPPAGE_FRACTION,
) -> Decimal:
    """Ask is half a spread above the next open. The fill is that ask plus slippage."""
    ask = next_open * (Decimal("1") + spread_fraction / Decimal("2"))
    return ask * (Decimal("1") + slippage_fraction)


def run_backtest(
    bars: list[Bar],
    symbol: Symbol,
    *,
    settings: Settings,
    starting_equity: Decimal | None = None,
) -> BacktestResult:
    """One long at a time. Stop is checked before take-profit. Trail updates after the bar survives."""
    ordered = sorted(bars, key=lambda bar: bar.open_time)
    cash0 = starting_equity if starting_equity is not None else settings.paper_starting_equity
    params = _params(settings)
    risk = RiskManager()
    cash = cash0
    position: _Open | None = None
    trades: list[BacktestTrade] = []
    fills: list[BacktestFill] = []
    daily: dict[object, Decimal] = {}
    cooldown_until: datetime | None = None
    curve: list[tuple[datetime, Decimal]] = []
    if ordered:
        curve.append((ordered[0].open_time, cash0))

    for index in range(len(ordered) - 1):
        closed = ordered[: index + 1]
        nxt = ordered[index + 1]
        if position is not None:
            exited = _try_exit(
                position,
                nxt,
                symbol_name=symbol.symbol,
                slippage=settings.paper_slippage_fraction,
                fee_rate=settings.default_fee_rate,
            )
            if exited is not None:
                trade, proceeds = exited
                cash += proceeds
                trades.append(trade)
                fills.append(
                    BacktestFill(nxt.open_time, "sell", trade.exit_price, position.decision_open)
                )
                day = nxt.open_time.date()
                daily[day] = daily.get(day, Decimal("0")) + trade.net_pnl
                cooldown_until = nxt.open_time + timedelta(minutes=settings.cooldown_minutes)
                position = None
                curve.append((nxt.open_time, cash))
                continue
            _advance_trail(
                position,
                nxt.high,
                activation=settings.trailing_activation_atr,
                trail_mult=settings.trailing_atr_multiplier,
            )
            curve.append((nxt.open_time, cash + position.quantity * nxt.close))
            continue

        if cooldown_until is not None and nxt.open_time < cooldown_until:
            curve.append((nxt.open_time, cash))
            continue

        view = evaluate(closed, params)
        if not view.buy or view.atr is None or view.bar_open is None:
            curve.append((nxt.open_time, cash))
            continue
        book = _book(nxt.open, settings.max_spread_fraction)
        ask = book.best_ask
        if ask is None:
            curve.append((nxt.open_time, cash))
            continue
        decision = risk.assess(
            RiskInput(
                equity=cash,
                available_quote=cash,
                profile=settings.default_risk,
                fee_rate=settings.default_fee_rate,
                entry=ask.price,
                atr=view.atr,
                atr_sl_multiplier=params.atr_sl_multiplier,
                take_profit_rr=params.take_profit_rr,
                symbol=symbol,
                book=book,
                open_positions=0,
                has_position_on_symbol=False,
                daily_net_pnl=daily.get(nxt.open_time.date(), Decimal("0")),
                cooldown_active=False,
                duplicate_signal=False,
                signal_valid=True,
                api_ok=True,
                market_data_ok=True,
                data_stale=False,
                paused=False,
                emergency_stop=False,
                reconciliation_blocking=False,
                max_spread_fraction=settings.max_spread_fraction,
                max_slippage_fraction=settings.max_slippage_fraction,
                cash_reserve_fraction=settings.cash_reserve_fraction,
            )
        )
        if (
            not decision.allowed
            or decision.size is None
            or decision.stop is None
            or decision.take_profit is None
        ):
            curve.append((nxt.open_time, cash))
            continue
        fill = ask.price * (Decimal("1") + settings.paper_slippage_fraction)
        notional = decision.size * fill
        entry_fee = notional * settings.default_fee_rate
        if notional + entry_fee >= cash:
            curve.append((nxt.open_time, cash))
            continue
        cash -= notional + entry_fee
        decision_open = closed[-1].open_time
        position = _Open(
            quantity=decision.size,
            entry=fill,
            stop=decision.stop,
            take_profit=decision.take_profit,
            entry_fee=entry_fee,
            opened_at=nxt.open_time,
            peak=fill,
            trail=None,
            atr=view.atr,
            decision_open=decision_open,
        )
        fills.append(BacktestFill(nxt.open_time, "buy", fill, decision_open))
        stopped = _try_exit(
            position,
            nxt,
            symbol_name=symbol.symbol,
            slippage=settings.paper_slippage_fraction,
            fee_rate=settings.default_fee_rate,
        )
        if stopped is not None:
            trade, proceeds = stopped
            cash += proceeds
            trades.append(trade)
            fills.append(BacktestFill(nxt.open_time, "sell", trade.exit_price, decision_open))
            day = nxt.open_time.date()
            daily[day] = daily.get(day, Decimal("0")) + trade.net_pnl
            cooldown_until = nxt.open_time + timedelta(minutes=settings.cooldown_minutes)
            position = None
            curve.append((nxt.open_time, cash))
            continue
        _advance_trail(
            position,
            nxt.high,
            activation=settings.trailing_activation_atr,
            trail_mult=settings.trailing_atr_multiplier,
        )
        curve.append((nxt.open_time, cash + position.quantity * nxt.close))

    ending = cash
    if position is not None and ordered:
        ending = cash + position.quantity * ordered[-1].close
        if not curve or curve[-1][0] != ordered[-1].open_time:
            curve.append((ordered[-1].open_time, ending))
        else:
            curve[-1] = (ordered[-1].open_time, ending)
    points = [
        TradePoint(net_pnl=trade.net_pnl, gross_pnl=trade.gross_pnl, fees=trade.fees, r_multiple=trade.r_multiple)
        for trade in trades
    ]
    performance = summarize(points, cash0)
    max_dd, current_dd = equity_drawdown([value for _, value in curve] or [cash0])
    return BacktestResult(
        symbol=symbol.symbol,
        starting_equity=cash0,
        ending_equity=ending,
        trades=tuple(trades),
        fills=tuple(fills),
        equity_curve=tuple(curve),
        performance=performance,
        max_drawdown=max_dd,
        current_drawdown=current_dd,
    )


def _params(settings: Settings) -> StrategyParams:
    return StrategyParams(
        volume_ma_period=settings.volume_ma_period,
        min_volume_ratio=settings.min_volume_ratio,
        rsi_low=settings.rsi_entry_min,
        rsi_high=settings.rsi_entry_max,
        atr_sl_multiplier=settings.atr_sl_multiplier,
        take_profit_rr=settings.take_profit_rr,
    )


def _book(mid: Decimal, spread_fraction: Decimal) -> BookSnapshot:
    half = spread_fraction / Decimal("2")
    ask = mid * (Decimal("1") + half)
    bid = mid * (Decimal("1") - half)
    if bid <= 0:
        bid = mid / Decimal("2")
    depth = Decimal("100000000")
    return sort_book([BookLevel(bid, depth)], [BookLevel(ask, depth)])


def _try_exit(
    position: _Open,
    bar: Bar,
    *,
    symbol_name: str,
    slippage: Decimal,
    fee_rate: Decimal,
) -> tuple[BacktestTrade, Decimal] | None:
    stop = position.stop
    reason = "sl"
    if position.trail is not None and position.trail > stop:
        stop = position.trail
        reason = "trailing"
    raw: Decimal | None = None
    if bar.open <= stop:
        raw = bar.open
    elif bar.low <= stop:
        raw = stop
    elif bar.high >= position.take_profit:
        raw = bar.open if bar.open >= position.take_profit else position.take_profit
        reason = "tp"
    if raw is None:
        return None
    exit_price = raw * (Decimal("1") - slippage)
    exit_notional = position.quantity * exit_price
    exit_fee = exit_notional * fee_rate
    gross = (exit_price - position.entry) * position.quantity
    fees = position.entry_fee + exit_fee
    net = gross - fees
    risk_cash = (position.entry - position.stop) * position.quantity
    r_multiple = None if risk_cash <= 0 else net / risk_cash
    trade = BacktestTrade(
        symbol=symbol_name,
        entry_time=position.opened_at,
        exit_time=bar.open_time,
        entry_price=position.entry,
        exit_price=exit_price,
        quantity=position.quantity,
        stop=position.stop,
        gross_pnl=gross,
        fees=fees,
        net_pnl=net,
        r_multiple=r_multiple,
        reason=reason,
    )
    proceeds = exit_notional - exit_fee
    return trade, proceeds


def _advance_trail(position: _Open, high: Decimal, *, activation: Decimal, trail_mult: Decimal) -> None:
    if position.atr <= 0:
        return
    if high > position.peak:
        position.peak = high
    if position.peak < position.entry + (position.atr * activation):
        return
    candidate = position.peak - (position.atr * trail_mult)
    if position.trail is None or candidate > position.trail:
        position.trail = candidate
