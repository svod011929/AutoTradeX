"""Next-bar replay for pluggable research strategies.

A signal is read at the close of bar ``i`` and filled on bar ``i + 1``.
The close of the fill bar is not an entry price. A resting stop is tested
before anything profitable on that bar. A gap through the stop fills at the open.
Maker entries fill only when the low is strictly below the limit.
"""

from dataclasses import dataclass
from decimal import Decimal

from backtest.metrics import equity_drawdown
from core.trading_policy import CASH_RESERVE_FRACTION
from trading.market import Bar

from research.costs import (
    MAKER_FEE,
    TAKER_FEE,
    limit_buy_fills,
    stop_trigger,
    taker_buy_price,
    taker_sell_price,
)
from research.strategies.base import Action, EntryStrategy

STARTING_EQUITY = Decimal("1000")
RISK_FRACTION = Decimal("0.01")


@dataclass(frozen=True)
class ResearchTrade:
    entry_time: object
    exit_time: object
    entry_price: Decimal
    exit_price: Decimal
    quantity: Decimal
    net_pnl: Decimal
    fees: Decimal
    slippage: Decimal
    r_multiple: Decimal | None
    reason: str


@dataclass(frozen=True)
class ResearchResult:
    trades: tuple[ResearchTrade, ...]
    ending_equity: Decimal
    max_drawdown: Decimal
    net_return: Decimal
    cagr: Decimal | None
    return_over_drawdown: Decimal | None
    calmar: Decimal | None
    win_rate: Decimal | None
    profit_factor: Decimal | None
    average_r: Decimal | None
    time_in_market: Decimal
    costs: Decimal
    signals: int
    fills: int
    fill_rate: Decimal | None


@dataclass
class _Position:
    quantity: Decimal
    entry_price: Decimal
    entry_fee: Decimal
    entry_slippage: Decimal
    stop: Decimal | None
    resting_stop: bool
    held_high: Decimal
    opened_at: object
    risk_price: Decimal
    risk_cash: Decimal


@dataclass
class _Pending:
    kind: str
    signal_index: int
    limit: Decimal | None
    timeout: int
    stop: Decimal | None
    resting_stop: bool
    risk_mode: str
    risk_price: Decimal


def run_strategy(
    bars: list[Bar],
    strategy: EntryStrategy,
    *,
    start: int,
    end: int,
    execution: str = "market",
    offset_atr: Decimal = Decimal("0.5"),
    timeout: int = 4,
    atr_at: list[Decimal | None] | None = None,
    starting_equity: Decimal = STARTING_EQUITY,
) -> ResearchResult:
    """Replay signals whose index is in ``[start, end)``. Indicators may read earlier bars."""
    if execution not in {"market", "maker"}:
        raise ValueError("execution must be market or maker")
    strategy.sync(bars)
    cash = starting_equity
    position: _Position | None = None
    pending: _Pending | None = None
    exit_at_open = False
    trades: list[ResearchTrade] = []
    curve: list[Decimal] = []
    in_market = 0
    counted = 0
    signals = 0
    fills = 0
    costs = Decimal("0")
    last = min(len(bars) - 1, end)
    for index in range(1, last + 1):
        bar = bars[index]
        if position is not None and exit_at_open:
            cash, position, trade, cost = _close_market(position, bar.open, bar.open_time, cash, "signal")
            trades.append(trade)
            costs += cost
            exit_at_open = False
        if position is not None and position.resting_stop and position.stop is not None:
            triggered = stop_trigger(position.stop, bar)
            if triggered is not None:
                cash, position, trade, cost = _close_market(position, triggered, bar.open_time, cash, "stop")
                trades.append(trade)
                costs += cost
                exit_at_open = False
        if position is None and pending is not None:
            opened = _try_pending(pending, bar, index, cash)
            if opened is not None:
                cash, position, spent = opened
                costs += spent
                fills += 1
                pending = None
                if position.resting_stop and position.stop is not None:
                    triggered = stop_trigger(position.stop, bar)
                    if triggered is not None:
                        cash, position, trade, cost = _close_market(
                            position, triggered, bar.open_time, cash, "stop"
                        )
                        trades.append(trade)
                        costs += cost
            elif pending.kind == "limit" and index > pending.signal_index + pending.timeout:
                pending = None
            elif pending.kind == "market" and index >= pending.signal_index + 1:
                pending = None
        if position is not None:
            position.held_high = bar.high if bar.high > position.held_high else position.held_high
            managed = strategy.on_position(index, position.held_high)
            if managed.stop is not None and position.resting_stop and position.stop is not None and managed.stop > position.stop:
                position.stop = managed.stop
            if managed.exit:
                exit_at_open = True
        if position is None and pending is None and start <= index < end:
            decision = strategy.action(index)
            if decision.enter:
                armed = _arm(decision, index, bars, execution, offset_atr, timeout, atr_at)
                if armed is not None:
                    signals += 1
                    pending = armed
        if start <= index < end:
            counted += 1
            if position is not None:
                in_market += 1
            marked = cash + (position.quantity * bar.close if position is not None else Decimal("0"))
            curve.append(marked)
    mark_at = min(len(bars) - 1, end)
    ending = cash + (position.quantity * bars[mark_at].close if position is not None else Decimal("0"))
    if not curve or curve[-1] != ending:
        curve.append(ending)
    span = _span_days(bars, start, end)
    return _pack(
        trades,
        ending,
        starting_equity,
        curve,
        in_market,
        counted,
        costs,
        signals,
        fills,
        span,
    )


def _arm(
    decision: Action,
    index: int,
    bars: list[Bar],
    execution: str,
    offset_atr: Decimal,
    timeout: int,
    atr_at: list[Decimal | None] | None,
) -> _Pending | None:
    risk_price = decision.stop if decision.stop is not None else bars[index].close
    if execution == "market":
        return _Pending(
            "market",
            index,
            None,
            0,
            decision.stop,
            decision.resting_stop,
            decision.risk_mode,
            risk_price,
        )
    limit = decision.limit
    if limit is None:
        width = None if atr_at is None or index >= len(atr_at) else atr_at[index]
        if width is None or width <= 0:
            return None
        limit = bars[index].close - (offset_atr * width)
    if limit <= 0:
        return None
    if decision.stop is not None and limit <= decision.stop:
        return None
    return _Pending(
        "limit",
        index,
        limit,
        timeout,
        decision.stop,
        decision.resting_stop,
        decision.risk_mode,
        risk_price,
    )


def _try_pending(
    pending: _Pending,
    bar: Bar,
    index: int,
    cash: Decimal,
) -> tuple[Decimal, _Position, Decimal] | None:
    if pending.kind == "market" and index == pending.signal_index + 1:
        return _open(cash, bar.open, bar, pending, taker=True)
    if (
        pending.kind == "limit"
        and pending.limit is not None
        and pending.signal_index < index <= pending.signal_index + pending.timeout
        and limit_buy_fills(pending.limit, bar)
    ):
        return _open(cash, pending.limit, bar, pending, taker=False)
    return None


def _open(
    cash: Decimal,
    raw: Decimal,
    bar: Bar,
    pending: _Pending,
    *,
    taker: bool,
) -> tuple[Decimal, _Position, Decimal] | None:
    if raw <= 0 or cash <= 0:
        return None
    if pending.resting_stop and pending.stop is not None and pending.stop >= raw:
        return None
    fee_rate = TAKER_FEE if taker else MAKER_FEE
    entry_px = taker_buy_price(raw) if taker else raw
    stop_px = pending.stop
    if pending.risk_mode == "risk" and stop_px is not None and stop_px < raw:
        slipped_stop = taker_sell_price(stop_px)
        loss = (entry_px - slipped_stop) + (fee_rate * entry_px) + (TAKER_FEE * slipped_stop)
        if loss <= 0:
            return None
        quantity = (cash * RISK_FRACTION) / loss
    else:
        quantity = None
    cap = cash * (Decimal("1") - CASH_RESERVE_FRACTION)
    unit = entry_px * (Decimal("1") + fee_rate)
    if unit <= 0:
        return None
    affordable = cap / unit
    if quantity is None or quantity > affordable:
        quantity = affordable
    if quantity <= 0:
        return None
    fee = entry_px * quantity * fee_rate
    notional = entry_px * quantity
    if notional + fee >= cash:
        return None
    risk_distance = entry_px - (stop_px if stop_px is not None else entry_px)
    risk_cash = risk_distance * quantity
    position = _Position(
        quantity=quantity,
        entry_price=entry_px,
        entry_fee=fee,
        entry_slippage=(entry_px - raw) * quantity,
        stop=pending.stop,
        resting_stop=pending.resting_stop,
        held_high=bar.high,
        opened_at=bar.open_time,
        risk_price=pending.risk_price,
        risk_cash=risk_cash if risk_cash > 0 else Decimal("0"),
    )
    return cash - notional - fee, position, fee + position.entry_slippage


def _close_market(
    position: _Position,
    raw: Decimal,
    when: object,
    cash: Decimal,
    reason: str,
) -> tuple[Decimal, None, ResearchTrade, Decimal]:
    exit_px = taker_sell_price(raw)
    exit_fee = exit_px * position.quantity * TAKER_FEE
    exit_slip = (raw - exit_px) * position.quantity
    proceeds = exit_px * position.quantity - exit_fee
    gross = (exit_px - position.entry_price) * position.quantity
    fees = position.entry_fee + exit_fee
    net = gross - fees
    r_multiple = None if position.risk_cash <= 0 else net / position.risk_cash
    trade = ResearchTrade(
        entry_time=position.opened_at,
        exit_time=when,
        entry_price=position.entry_price,
        exit_price=exit_px,
        quantity=position.quantity,
        net_pnl=net,
        fees=fees,
        slippage=position.entry_slippage + exit_slip,
        r_multiple=r_multiple,
        reason=reason,
    )
    return cash + proceeds, None, trade, exit_fee + exit_slip


def _span_days(bars: list[Bar], start: int, end: int) -> Decimal:
    if not bars:
        return Decimal("0")
    left = bars[min(start, len(bars) - 1)].open_time
    right = bars[min(max(end - 1, 0), len(bars) - 1)].open_time
    seconds = Decimal(str((right - left).total_seconds()))
    if seconds <= 0:
        return Decimal("0")
    return seconds / Decimal("86400")


def _pack(
    trades: list[ResearchTrade],
    ending: Decimal,
    starting: Decimal,
    curve: list[Decimal],
    in_market: int,
    counted: int,
    costs: Decimal,
    signals: int,
    fills: int,
    span_days: Decimal,
) -> ResearchResult:
    max_dd, _current = equity_drawdown(curve or [starting])
    net_return = (ending / starting) - Decimal("1") if starting > 0 else Decimal("0")
    cagr = _cagr(starting, ending, span_days)
    ratio = None if max_dd <= 0 else net_return / max_dd
    calmar = None if cagr is None or max_dd <= 0 else cagr / max_dd
    wins = [trade.net_pnl for trade in trades if trade.net_pnl > 0]
    losses = [trade.net_pnl for trade in trades if trade.net_pnl < 0]
    rs = [trade.r_multiple for trade in trades if trade.r_multiple is not None]
    count = len(trades)
    profit_factor = None
    if losses:
        loss_sum = abs(sum(losses, Decimal("0")))
        if loss_sum > 0:
            profit_factor = sum(wins, Decimal("0")) / loss_sum
    return ResearchResult(
        trades=tuple(trades),
        ending_equity=ending,
        max_drawdown=max_dd,
        net_return=net_return,
        cagr=cagr,
        return_over_drawdown=ratio,
        calmar=calmar,
        win_rate=(Decimal(len(wins)) / Decimal(count)) if count else None,
        profit_factor=profit_factor,
        average_r=(sum(rs, Decimal("0")) / Decimal(len(rs))) if rs else None,
        time_in_market=(Decimal(in_market) / Decimal(counted)) if counted else Decimal("0"),
        costs=costs,
        signals=signals,
        fills=fills,
        fill_rate=(Decimal(fills) / Decimal(signals)) if signals else None,
    )


def _cagr(starting: Decimal, ending: Decimal, span_days: Decimal) -> Decimal | None:
    if starting <= 0 or ending <= 0 or span_days <= 0:
        return None
    growth = float(ending) / float(starting)
    if growth <= 0:
        return None
    return Decimal(str(growth ** (365.0 / float(span_days)) - 1.0))
