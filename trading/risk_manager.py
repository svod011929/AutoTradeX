"""Pre-order checklist and risk-based size.

The size is the risk budget divided by the cash lost if the stop fills,
including the entry fee, the exit fee, and the expected book and paper
slippage. The 0.3% slippage cap stays a gate. The whole available balance
is never spent. A trade whose net reward/risk after those costs is below
the configured threshold is skipped.
"""

from dataclasses import dataclass
from decimal import Decimal

from core.config import RISK_PROFILES, RiskName
from trading.market import BookSnapshot, walk_book
from xrocket.models import Symbol
from xrocket.exceptions import PrecisionError
from xrocket.precision import floor_funds, floor_to_increment, format_decimal, quote_step


@dataclass(frozen=True)
class RiskInput:
    equity: Decimal
    available_quote: Decimal
    profile: RiskName
    fee_rate: Decimal
    entry: Decimal
    atr: Decimal
    atr_sl_multiplier: Decimal
    take_profit_rr: Decimal
    symbol: Symbol
    book: BookSnapshot
    open_positions: int
    has_position_on_symbol: bool
    daily_net_pnl: Decimal
    cooldown_active: bool
    duplicate_signal: bool
    signal_valid: bool
    api_ok: bool
    market_data_ok: bool
    data_stale: bool
    paused: bool
    emergency_stop: bool
    reconciliation_blocking: bool
    max_spread_fraction: Decimal
    max_slippage_fraction: Decimal
    cash_reserve_fraction: Decimal
    paper_slippage_fraction: Decimal = Decimal("0")
    min_net_reward_risk: Decimal = Decimal("0")


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reasons: tuple[str, ...]
    size: Decimal | None
    funds: Decimal | None
    stop: Decimal | None
    take_profit: Decimal | None
    entry: Decimal | None


class RiskManager:
    def assess(self, item: RiskInput) -> RiskDecision:
        reasons: list[str] = []
        self._gates(item, reasons)
        stop, take_profit = self._targets(item, reasons)
        size, funds = self._size(item, stop, take_profit, reasons) if stop is not None and take_profit is not None else (None, None)
        if size is None or funds is None:
            if (
                "quantity" not in reasons
                and "precision" not in reasons
                and "net_rr" not in reasons
                and "sl" not in reasons
                and "tp" not in reasons
                and stop is not None
            ):
                reasons.append("quantity")
        self._book_checks(item, size, reasons)
        if reasons:
            return RiskDecision(False, tuple(dict.fromkeys(reasons)), None, None, stop, take_profit, item.entry)
        return RiskDecision(True, (), size, funds, stop, take_profit, item.entry)

    def _gates(self, item: RiskInput, reasons: list[str]) -> None:
        if not item.api_ok:
            reasons.append("api")
        if item.data_stale:
            reasons.append("stale")
        if not item.market_data_ok:
            reasons.append("ws")
        if not item.symbol.enable_trading:
            reasons.append("symbol")
        if item.paused:
            reasons.append("paused")
        if item.emergency_stop:
            reasons.append("emergency_stop")
        if item.reconciliation_blocking:
            reasons.append("reconciliation")
        if item.has_position_on_symbol:
            reasons.append("existing_position")
        profile = RISK_PROFILES[item.profile]
        if item.open_positions >= profile.max_positions:
            reasons.append("position_limit")
        loss_limit = -(item.equity * profile.daily_loss)
        if item.daily_net_pnl <= loss_limit:
            reasons.append("daily_loss")
        if item.cooldown_active:
            reasons.append("cooldown")
        if item.duplicate_signal:
            reasons.append("duplicate")
        if not item.signal_valid:
            reasons.append("signal")
        if item.equity <= 0 or item.available_quote <= 0:
            reasons.append("balance")

    def _targets(self, item: RiskInput, reasons: list[str]) -> tuple[Decimal | None, Decimal | None]:
        if item.entry <= 0 or item.atr <= 0 or item.atr_sl_multiplier <= 0 or item.take_profit_rr <= 0:
            reasons.append("sl")
            reasons.append("tp")
            return None, None
        stop = item.entry - (item.atr * item.atr_sl_multiplier)
        if stop <= 0 or stop >= item.entry:
            reasons.append("sl")
            return None, None
        take_profit = item.entry + (item.entry - stop) * item.take_profit_rr
        if take_profit <= item.entry:
            reasons.append("tp")
            return stop, None
        profile = RISK_PROFILES[item.profile]
        if profile.risk_per_trade <= 0:
            reasons.append("risk")
        return stop, take_profit

    def _size(
        self,
        item: RiskInput,
        stop: Decimal | None,
        take_profit: Decimal | None,
        reasons: list[str],
    ) -> tuple[Decimal | None, Decimal | None]:
        if stop is None or take_profit is None or "balance" in reasons or "risk" in reasons:
            return None, None
        profile = RISK_PROFILES[item.profile]
        provisional = self._loss_quote(item, stop, take_profit, Decimal("0"))
        if provisional is None:
            reasons.append("sl")
            return None, None
        _entry_px, loss, _profit = provisional
        if loss <= 0:
            reasons.append("sl")
            return None, None
        risk_amount = item.equity * profile.risk_per_trade
        probe = floor_to_increment(risk_amount / loss, item.symbol.base_increment)
        book_slip = _entry_book_slippage(item, probe)
        priced = self._loss_quote(item, stop, take_profit, book_slip)
        if priced is None:
            reasons.append("sl")
            return None, None
        entry_px, loss, profit = priced
        if loss <= 0:
            reasons.append("sl")
            return None, None
        if item.min_net_reward_risk > 0 and (profit <= 0 or profit / loss < item.min_net_reward_risk):
            reasons.append("net_rr")
            return None, None
        size = floor_to_increment(risk_amount / loss, item.symbol.base_increment)
        size, funds = self._fit_balance(item, size, entry_px)
        if size is None or funds is None:
            reasons.append("balance")
            return None, None
        if size < item.symbol.base_min_size or size > item.symbol.base_max_size:
            reasons.append("precision")
            return None, None
        if funds < item.symbol.quote_min_size or funds > item.symbol.quote_max_size:
            reasons.append("precision")
            return None, None
        if funds >= item.available_quote:
            reasons.append("balance")
            return None, None
        return size, funds

    def _loss_quote(
        self,
        item: RiskInput,
        stop: Decimal,
        take_profit: Decimal,
        book_slippage: Decimal,
    ) -> tuple[Decimal, Decimal, Decimal] | None:
        entry_px, stop_px, take_profit_px = execution_prices(
            item.entry,
            stop,
            take_profit,
            book_slippage=book_slippage,
            paper_slippage=item.paper_slippage_fraction,
        )
        if stop_px <= 0 or stop_px >= entry_px:
            return None
        loss = stop_loss_per_unit(entry_px, stop_px, item.fee_rate)
        profit = take_profit_per_unit(entry_px, take_profit_px, item.fee_rate)
        return entry_px, loss, profit

    def _fit_balance(
        self,
        item: RiskInput,
        size: Decimal,
        entry_px: Decimal,
    ) -> tuple[Decimal | None, Decimal | None]:
        reserve = item.available_quote * item.cash_reserve_fraction
        cap = item.available_quote - reserve
        if cap <= 0 or cap >= item.available_quote:
            cap = item.available_quote - quote_step(item.symbol.quote_min_size)
        if cap <= 0:
            return None, None
        unit = entry_px * (Decimal("1") + item.fee_rate)
        if unit <= 0:
            return None, None
        affordable = floor_to_increment(cap / unit, item.symbol.base_increment)
        if size > affordable:
            size = affordable
        if size <= 0:
            return None, None
        funds = size * unit
        try:
            funds = floor_funds(funds, item.symbol)
        except PrecisionError:
            return None, None
        if funds >= item.available_quote or funds <= 0:
            return None, None
        return size, funds

    def _book_checks(self, item: RiskInput, size: Decimal | None, reasons: list[str]) -> None:
        if not item.book.is_full_snapshot:
            reasons.append("orderbook")
            return
        bid = item.book.best_bid
        ask = item.book.best_ask
        if bid is None or ask is None or bid.price <= 0 or ask.price <= 0 or ask.price < bid.price:
            reasons.append("spread")
            return
        mid = (ask.price + bid.price) / Decimal("2")
        spread = (ask.price - bid.price) / mid
        if spread > item.max_spread_fraction:
            reasons.append("spread")
        if size is None or size <= 0:
            return
        vwap = walk_book(item.book.asks, size)
        if vwap is None:
            reasons.append("liquidity")
            return
        slippage = (vwap - ask.price) / ask.price
        if slippage > item.max_slippage_fraction:
            reasons.append("slippage")


def describe_size(size: Decimal | None) -> str:
    if size is None:
        return "none"
    return format_decimal(size)


def execution_prices(
    entry: Decimal,
    stop: Decimal,
    take_profit: Decimal,
    *,
    book_slippage: Decimal,
    paper_slippage: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    """Entry pays the ask walk and paper slippage. Stop and take pay the bid side once."""
    up = (Decimal("1") + book_slippage) * (Decimal("1") + paper_slippage)
    down = (Decimal("1") - book_slippage) * (Decimal("1") - paper_slippage)
    return entry * up, stop * down, take_profit * down


def stop_loss_per_unit(entry_px: Decimal, stop_px: Decimal, fee_rate: Decimal) -> Decimal:
    """Quote lost on one base unit if the stop fills, including both fees."""
    return (entry_px - stop_px) + (fee_rate * entry_px) + (fee_rate * stop_px)


def take_profit_per_unit(entry_px: Decimal, take_profit_px: Decimal, fee_rate: Decimal) -> Decimal:
    """Quote gained on one base unit if the take-profit fills, after both fees."""
    return (take_profit_px - entry_px) - (fee_rate * entry_px) - (fee_rate * take_profit_px)


def _entry_book_slippage(item: RiskInput, size: Decimal) -> Decimal:
    ask = item.book.best_ask
    if ask is None or ask.price <= 0 or size <= 0:
        return Decimal("0")
    vwap = walk_book(item.book.asks, size)
    if vwap is None:
        return Decimal("0")
    slip = (vwap - ask.price) / ask.price
    if slip <= 0:
        return Decimal("0")
    return slip
