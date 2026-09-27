"""Performance numbers shared by the backtest report and the Telegram stats screen."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class TradePoint:
    net_pnl: Decimal
    gross_pnl: Decimal
    fees: Decimal
    r_multiple: Decimal | None = None


@dataclass(frozen=True)
class Performance:
    trades: int
    wins: int
    losses: int
    win_rate: Decimal | None
    gross_pnl: Decimal
    fees: Decimal
    net_pnl: Decimal
    avg_win: Decimal | None
    avg_loss: Decimal | None
    profit_factor: Decimal | None
    max_drawdown: Decimal
    current_drawdown: Decimal
    average_r: Decimal | None
    best: Decimal | None
    worst: Decimal | None
    equity: tuple[Decimal, ...]


def summarize(points: list[TradePoint], starting_equity: Decimal) -> Performance:
    """Profit factor and drawdown use net P&L. A series with no losses has no profit factor."""
    equity = [starting_equity]
    cash = starting_equity
    wins: list[Decimal] = []
    losses: list[Decimal] = []
    rs: list[Decimal] = []
    gross = Decimal("0")
    fees = Decimal("0")
    for point in points:
        cash += point.net_pnl
        equity.append(cash)
        gross += point.gross_pnl
        fees += point.fees
        if point.net_pnl > 0:
            wins.append(point.net_pnl)
        elif point.net_pnl < 0:
            losses.append(point.net_pnl)
        if point.r_multiple is not None:
            rs.append(point.r_multiple)
    net = cash - starting_equity
    count = len(points)
    return Performance(
        trades=count,
        wins=len(wins),
        losses=len(losses),
        win_rate=(Decimal(len(wins)) / Decimal(count)) if count else None,
        gross_pnl=gross,
        fees=fees,
        net_pnl=net,
        avg_win=_mean(wins),
        avg_loss=_mean(losses),
        profit_factor=_profit_factor(wins, losses),
        max_drawdown=_max_drawdown(equity),
        current_drawdown=_current_drawdown(equity),
        average_r=_mean(rs),
        best=max(point.net_pnl for point in points) if points else None,
        worst=min(point.net_pnl for point in points) if points else None,
        equity=tuple(equity),
    )


def _mean(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum(values, Decimal("0")) / Decimal(len(values))


def _profit_factor(wins: list[Decimal], losses: list[Decimal]) -> Decimal | None:
    if not losses:
        return None
    loss = abs(sum(losses, Decimal("0")))
    if loss == 0:
        return None
    return sum(wins, Decimal("0")) / loss


def equity_drawdown(equity: list[Decimal]) -> tuple[Decimal, Decimal]:
    """Max and current drawdown as fractions of the peak. Empty input is zero."""
    if not equity:
        return Decimal("0"), Decimal("0")
    return _max_drawdown(equity), _current_drawdown(equity)


def _max_drawdown(equity: list[Decimal]) -> Decimal:
    peak = equity[0]
    worst = Decimal("0")
    for value in equity:
        if value > peak:
            peak = value
        if peak > 0:
            dip = (peak - value) / peak
            if dip > worst:
                worst = dip
    return worst


def _current_drawdown(equity: list[Decimal]) -> Decimal:
    peak = max(equity)
    last = equity[-1]
    if peak <= 0:
        return Decimal("0")
    return (peak - last) / peak
