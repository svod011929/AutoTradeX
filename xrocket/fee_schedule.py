"""Public taker and maker rates from GET /api/v1/trade-fees.

The schedule is cached and refreshed on a timer. A failed refresh keeps the
last rates. With no rates at all, market orders use the taker fallback 0.3%
and a warning is logged. The debit currency is not in this payload.
"""

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Protocol

from core.trading_policy import DEFAULT_FEE_RATE, DEFAULT_MAKER_FEE_RATE, fee_rate_to_fraction
from xrocket.models import TradeFee

logger = logging.getLogger("autotrade.fees")


class TradeFeeSource(Protocol):
    async def get_trade_fees(self, symbols: list[str] | None = None) -> list[TradeFee]: ...


class FeeSchedule:
    def __init__(self, *, refresh_seconds: int, treat_as_fraction: bool = True) -> None:
        self._refresh_after = timedelta(seconds=refresh_seconds)
        self._treat_as_fraction = treat_as_fraction
        self._taker: dict[str, Decimal] = {}
        self._maker: dict[str, Decimal] = {}
        self._fetched_at: datetime | None = None
        self._fallback_warned = False

    @property
    def has_rates(self) -> bool:
        return bool(self._taker)

    def needs_refresh(self, now: datetime) -> bool:
        if self._fetched_at is None:
            return True
        return now - self._fetched_at >= self._refresh_after

    def taker_rate(self, symbol: str | None = None) -> Decimal:
        """Taker fraction for a market order. Last known rate, else 0.3%."""
        if symbol is not None and symbol in self._taker:
            return self._taker[symbol]
        if self._taker:
            return next(iter(self._taker.values()))
        self._warn_fallback()
        return DEFAULT_FEE_RATE

    def maker_rate(self, symbol: str | None = None) -> Decimal:
        if symbol is not None and symbol in self._maker:
            return self._maker[symbol]
        if self._maker:
            return next(iter(self._maker.values()))
        return DEFAULT_MAKER_FEE_RATE

    async def refresh(self, client: TradeFeeSource, now: datetime | None = None) -> bool:
        """Load the public schedule. False means the previous rates stay in place."""
        moment = now or datetime.now(timezone.utc)
        self._fetched_at = moment
        try:
            fees = await client.get_trade_fees()
        except Exception as exc:
            self._note_failure(exc)
            return False
        taker: dict[str, Decimal] = {}
        maker: dict[str, Decimal] = {}
        for fee in fees:
            taker[fee.symbol] = fee_rate_to_fraction(fee.standard.taker, self._treat_as_fraction)
            maker[fee.symbol] = fee_rate_to_fraction(fee.standard.maker, self._treat_as_fraction)
        if not taker:
            logger.warning("public trade-fees response had no rates; keeping the last known taker or the 0.003 fallback")
            return False
        self._taker = taker
        self._maker = maker
        self._fallback_warned = False
        logger.info("public trade-fees updated for %s symbols", len(taker))
        return True

    def _note_failure(self, exc: Exception) -> None:
        if self._taker:
            logger.warning("public trade-fees refresh failed (%s); keeping the last known taker rate", exc)
            return
        self._warn_fallback()

    def _warn_fallback(self) -> None:
        if self._fallback_warned:
            return
        self._fallback_warned = True
        logger.warning("public trade-fees unavailable; using taker fallback 0.003")
