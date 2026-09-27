"""Pre-order checklist and risk-based size.

Size is risk_amount / stop distance, then trimmed by balance, fees, exchange
limits, and the book. The whole available balance is never spent.
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
        size, funds = self._size(item, stop, reasons) if stop is not None else (None, None)
        if size is None or funds is None:
            if "quantity" not in reasons and "precision" not in reasons and stop is not None:
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
        reasons: list[str],
    ) -> tuple[Decimal | None, Decimal | None]:
        if stop is None or "balance" in reasons or "risk" in reasons:
            return None, None
        profile = RISK_PROFILES[item.profile]
        distance = item.entry - stop
        if distance <= 0:
            reasons.append("sl")
            return None, None
        risk_amount = item.equity * profile.risk_per_trade
        raw_size = risk_amount / distance
        size = floor_to_increment(raw_size, item.symbol.base_increment)
        size, funds = self._fit_balance(item, size)
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

    def _fit_balance(self, item: RiskInput, size: Decimal) -> tuple[Decimal | None, Decimal | None]:
        reserve = item.available_quote * item.cash_reserve_fraction
        cap = item.available_quote - reserve
        if cap <= 0 or cap >= item.available_quote:
            cap = item.available_quote - quote_step(item.symbol.quote_min_size)
        if cap <= 0:
            return None, None
        unit = item.entry * (Decimal("1") + item.fee_rate)
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
