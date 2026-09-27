"""EMA, RSI, ATR, and volume average against hand-computed values."""

from decimal import Decimal

from trading.indicators import atr, ema, rsi, sma, volume_ma


def test_ema_seeds_with_sma_then_smooths() -> None:
    values = ema([Decimal(item) for item in (1, 2, 3, 4, 5)], 3)
    assert values == [Decimal("2"), Decimal("3"), Decimal("4")]


def test_rsi_wilder_matches_the_worked_example() -> None:
    closes = [Decimal(item) for item in (10, 11, 12, 11, 10, 11)]
    values = rsi(closes, 3)
    assert values[0] == Decimal(200) / Decimal(3)
    assert values[1] == Decimal(400) / Decimal(9)
    # The third step is 1700/27. Decimal division keeps 28 digits, so compare 8 places.
    places = Decimal("0.00000001")
    assert values[2].quantize(places) == (Decimal(1700) / Decimal(27)).quantize(places)


def test_rsi_is_100_when_there_is_no_loss() -> None:
    closes = [Decimal(item) for item in (1, 2, 3, 4)]
    assert rsi(closes, 3) == [Decimal("100")]


def test_atr_wilder_matches_the_worked_example() -> None:
    highs = [Decimal(item) for item in (10, 12, 11)]
    lows = [Decimal(item) for item in (8, 9, 10)]
    closes = [Decimal(item) for item in (9, 11, 10)]
    assert atr(highs, lows, closes, 2) == [Decimal("2.5"), Decimal("1.75")]


def test_volume_average_is_a_simple_moving_average() -> None:
    volumes = [Decimal(item) for item in (1, 2, 3, 4)]
    assert volume_ma(volumes, 2) == sma(volumes, 2)
    assert volume_ma(volumes, 2) == [Decimal("1.5"), Decimal("2.5"), Decimal("3.5")]
