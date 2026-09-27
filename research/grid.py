"""Spot grid inside a bounded range.

Each buy is a maker limit. A level fills only when the low is strictly below
it, and a sell fills only when the high is strictly above the target. The
step is at least 0.5%, above a 0.2% + 0.2% maker round trip. One bar never
both buys and sells. A break of the range low is a taker stop and is handled
before any sell. Equity is cash plus coin marked at the close, so an open
inventory drawdown is in the curve.
"""

from dataclasses import dataclass
from decimal import Decimal

from core.trading_policy import CASH_RESERVE_FRACTION
from trading.market import Bar

from research.costs import MIN_GRID_STEP, MAKER_FEE, TAKER_FEE, limit_buy_fills, limit_sell_fills, stop_trigger, taker_sell_price
from research.engine import STARTING_EQUITY, ResearchResult, ResearchTrade, _pack
from research.strategies.base import TimeframeName, bars_per_day

MAX_RANGE_WIDTH = Decimal("0.20")


@dataclass(frozen=True)
class GridParams:
    timeframe: TimeframeName
    step: Decimal
    max_levels: int
    lookback_days: int

    def __post_init__(self) -> None:
        if self.step < MIN_GRID_STEP:
            raise ValueError("grid step must be at least 0.5%")
        if self.max_levels < 2 or self.lookback_days < 2:
            raise ValueError("grid needs at least two levels and two days of range")


@dataclass
class _Lot:
    price: Decimal
    quantity: Decimal
    fee: Decimal
    opened_at: object


@dataclass
class _Range:
    low: Decimal
    high: Decimal
    anchor: int
    levels: tuple[Decimal, ...]


def run_grid(
    bars: list[Bar],
    params: GridParams,
    *,
    start: int,
    end: int,
    starting_equity: Decimal = STARTING_EQUITY,
) -> ResearchResult:
    lookback = params.lookback_days * bars_per_day(params.timeframe)
    cash = starting_equity
    lots: list[_Lot] = []
    zone: _Range | None = None
    trades: list[ResearchTrade] = []
    curve: list[Decimal] = []
    in_market = 0
    counted = 0
    signals = 0
    fills = 0
    costs = Decimal("0")
    resting: set[Decimal] = set()
    last = min(len(bars) - 1, end)
    for index in range(1, last + 1):
        bar = bars[index]
        if index < start:
            if zone is None or index - 1 - zone.anchor >= lookback:
                zone = _build_range(bars, index - 1, lookback, params.step)
            continue
        if not (start <= index < end):
            continue
        if zone is not None:
            held = {lot.price for lot in lots}
            for level in zone.levels:
                if level in held or level in resting:
                    continue
                resting.add(level)
                signals += 1
        if zone is not None and lots and _broken(zone.low, bar):
            raw = stop_trigger(zone.low, bar)
            if raw is None:
                raw = bar.open
            cash, closed, cost = _liquidate(lots, raw, bar.open_time, cash)
            trades.extend(closed)
            costs += cost
            lots = []
            zone = None
            resting.clear()
        elif zone is not None:
            cash, lots, filled, cost, trade_rows = _grid_bar(
                bar,
                zone,
                lots,
                cash,
                starting_equity,
                params,
            )
            trades.extend(trade_rows)
            costs += cost
            fills += filled
            for lot in lots:
                resting.discard(lot.price)
        if not lots and (zone is None or index - 1 - zone.anchor >= lookback):
            built = _build_range(bars, index - 1, lookback, params.step)
            if built is not None and built.anchor != (zone.anchor if zone is not None else -1):
                zone = built
                resting.clear()
        counted += 1
        if lots:
            in_market += 1
        quantity = sum((lot.quantity for lot in lots), Decimal("0"))
        curve.append(cash + quantity * bar.close)
    ending = cash + (
        sum((lot.quantity for lot in lots), Decimal("0")) * bars[min(len(bars) - 1, max(end - 1, 0))].close
        if lots
        else Decimal("0")
    )
    if not curve or curve[-1] != ending:
        curve.append(ending)
    span = _days(bars, start, end)
    return _pack(trades, ending, starting_equity, curve, in_market, counted, costs, signals, fills, span)


def _broken(floor: Decimal, bar: Bar) -> bool:
    return bar.open <= floor or bar.low <= floor


def _build_range(bars: list[Bar], index: int, lookback: int, step: Decimal) -> _Range | None:
    if index < lookback:
        return None
    window = bars[index - lookback + 1 : index + 1]
    low = min(bar.low for bar in window)
    high = max(bar.high for bar in window)
    if low <= 0 or high <= low:
        return None
    width = (high - low) / low
    if width < step * 2 or width > MAX_RANGE_WIDTH:
        return None
    levels: list[Decimal] = []
    price = low
    while price < high and len(levels) < 64:
        levels.append(price)
        price = price * (Decimal("1") + step)
    # Only limits that were already below the close are resting buys.
    buyable = [level for level in levels if level < bars[index].close]
    if len(levels) < 2 or not buyable:
        return None
    return _Range(low, high, index, tuple(buyable))


def _grid_bar(
    bar: Bar,
    zone: _Range,
    lots: list[_Lot],
    cash: Decimal,
    starting_equity: Decimal,
    params: GridParams,
) -> tuple[Decimal, list[_Lot], int, Decimal, list[ResearchTrade]]:
    held = {lot.price for lot in lots}
    buy_levels = [level for level in zone.levels if level not in held and limit_buy_fills(level, bar)]
    sellable = [lot for lot in lots if limit_sell_fills(lot.price * (Decimal("1") + params.step), bar)]
    if buy_levels and sellable:
        sellable = []
    trades: list[ResearchTrade] = []
    cost = Decimal("0")
    filled = 0
    if sellable:
        cash, lots, closed, sold_cost = _sell_lots(sellable, lots, params.step, bar.open_time, cash)
        trades.extend(closed)
        cost += sold_cost
        return cash, lots, filled, cost, trades
    room = params.max_levels - len(lots)
    for level in sorted(buy_levels, reverse=True):
        if room <= 0:
            break
        opened = _buy_lot(level, cash, starting_equity, params.max_levels, bar.open_time)
        if opened is None:
            continue
        cash, lot, fee = opened
        lots.append(lot)
        cost += fee
        filled += 1
        room -= 1
    return cash, lots, filled, cost, trades


def _buy_lot(
    price: Decimal,
    cash: Decimal,
    starting_equity: Decimal,
    max_levels: int,
    when: object,
) -> tuple[Decimal, _Lot, Decimal] | None:
    budget = starting_equity / Decimal(max_levels)
    cap = cash * (Decimal("1") - CASH_RESERVE_FRACTION)
    if budget > cap:
        budget = cap
    unit = price * (Decimal("1") + MAKER_FEE)
    if unit <= 0 or budget <= 0:
        return None
    quantity = budget / unit
    fee = quantity * price * MAKER_FEE
    notional = quantity * price
    if quantity <= 0 or notional + fee >= cash:
        return None
    lot = _Lot(price, quantity, fee, when)
    return cash - notional - fee, lot, fee


def _sell_lots(
    selling: list[_Lot],
    lots: list[_Lot],
    step: Decimal,
    when: object,
    cash: Decimal,
) -> tuple[Decimal, list[_Lot], list[ResearchTrade], Decimal]:
    trades: list[ResearchTrade] = []
    cost = Decimal("0")
    kept = [lot for lot in lots if lot not in selling]
    for lot in selling:
        raw = lot.price * (Decimal("1") + step)
        fee = raw * lot.quantity * MAKER_FEE
        proceeds = raw * lot.quantity - fee
        gross = (raw - lot.price) * lot.quantity
        net = gross - lot.fee - fee
        risk = (lot.price - (lot.price / (Decimal("1") + step))) * lot.quantity
        trades.append(
            ResearchTrade(
                entry_time=lot.opened_at,
                exit_time=when,
                entry_price=lot.price,
                exit_price=raw,
                quantity=lot.quantity,
                net_pnl=net,
                fees=lot.fee + fee,
                slippage=Decimal("0"),
                r_multiple=None if risk <= 0 else net / risk,
                reason="grid",
            )
        )
        cash += proceeds
        cost += fee
    return cash, kept, trades, cost


def _liquidate(
    lots: list[_Lot],
    raw: Decimal,
    when: object,
    cash: Decimal,
) -> tuple[Decimal, list[ResearchTrade], Decimal]:
    exit_px = taker_sell_price(raw)
    trades: list[ResearchTrade] = []
    cost = Decimal("0")
    for lot in lots:
        exit_fee = exit_px * lot.quantity * TAKER_FEE
        slip = (raw - exit_px) * lot.quantity
        proceeds = exit_px * lot.quantity - exit_fee
        gross = (exit_px - lot.price) * lot.quantity
        net = gross - lot.fee - exit_fee
        distance = (lot.price - exit_px) * lot.quantity
        trades.append(
            ResearchTrade(
                entry_time=lot.opened_at,
                exit_time=when,
                entry_price=lot.price,
                exit_price=exit_px,
                quantity=lot.quantity,
                net_pnl=net,
                fees=lot.fee + exit_fee,
                slippage=slip,
                r_multiple=None if distance <= 0 else net / distance,
                reason="stop",
            )
        )
        cash += proceeds
        cost += exit_fee + slip
    return cash, trades, cost


def _days(bars: list[Bar], start: int, end: int) -> Decimal:
    if not bars:
        return Decimal("0")
    left = bars[min(start, len(bars) - 1)].open_time
    right = bars[min(max(end - 1, 0), len(bars) - 1)].open_time
    seconds = Decimal(str((right - left).total_seconds()))
    if seconds <= 0:
        return Decimal("0")
    return seconds / Decimal("86400")
