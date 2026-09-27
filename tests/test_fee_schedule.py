"""Public trade-fees cache, fallback, and the unauthenticated request."""

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from xrocket.fee_schedule import FeeSchedule
from xrocket.models import StandardFee, TradeFee
from xrocket.rest_client import XRocketRestClient

NOW = datetime(2026, 9, 27, tzinfo=timezone.utc)


def _fee(symbol: str = "BTC-USDT") -> TradeFee:
    return TradeFee(symbol=symbol, standard=StandardFee(taker=Decimal("0.003"), maker=Decimal("0.002")))


class _Source:
    def __init__(self, rows: list[TradeFee] | None = None, error: Exception | None = None) -> None:
        self.rows = rows if rows is not None else [_fee()]
        self.error = error
        self.calls = 0

    async def get_trade_fees(self, symbols: list[str] | None = None) -> list[TradeFee]:
        del symbols
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.rows


async def test_schedule_reads_taker_and_refreshes_on_a_timer() -> None:
    source = _Source()
    schedule = FeeSchedule(refresh_seconds=3600)
    assert schedule.needs_refresh(NOW)
    assert await schedule.refresh(source, NOW) is True
    assert schedule.taker_rate("BTC-USDT") == Decimal("0.003")
    assert schedule.maker_rate("ETH-USDT") == Decimal("0.002")
    assert schedule.needs_refresh(NOW + timedelta(minutes=30)) is False
    assert schedule.needs_refresh(NOW + timedelta(seconds=3600)) is True
    assert source.calls == 1


async def test_missing_schedule_falls_back_to_taker_with_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    schedule = FeeSchedule(refresh_seconds=3600)
    assert await schedule.refresh(_Source(error=RuntimeError("down")), NOW) is False
    assert schedule.has_rates is False
    assert schedule.taker_rate("BTC-USDT") == Decimal("0.003")
    assert schedule.taker_rate("ETH-USDT") == Decimal("0.003")
    assert "taker fallback 0.003" in caplog.text


async def test_failed_refresh_keeps_the_last_known_rate(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    source = _Source()
    schedule = FeeSchedule(refresh_seconds=60)
    assert await schedule.refresh(source, NOW) is True
    source.error = RuntimeError("down")
    assert await schedule.refresh(source, NOW + timedelta(minutes=2)) is False
    assert schedule.taker_rate("BTC-USDT") == Decimal("0.003")
    assert "last known" in caplog.text
    assert "taker fallback" not in caplog.text


async def test_trade_fees_request_is_public(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    async def fake(
        self: XRocketRestClient,
        method: str,
        path: str,
        *,
        private: bool,
        params: object = None,
        json_body: object = None,
        retry: bool = False,
    ) -> dict[str, object]:
        del self, method, params, json_body, retry
        seen["private"] = private
        seen["path"] = path
        return {"fees": [{"symbol": "BTC-USDT", "standard": {"taker": "0.003", "maker": "0.002"}}]}

    monkeypatch.setattr(XRocketRestClient, "_request", fake)
    client = XRocketRestClient("https://exchange.api.xrocket.exchange", token=None)
    fees = await client.get_trade_fees()
    assert seen == {"private": False, "path": "/api/v1/trade-fees"}
    assert fees[0].standard.taker == Decimal("0.003")
    assert client.fee_fraction(fees[0]) == Decimal("0.003")
