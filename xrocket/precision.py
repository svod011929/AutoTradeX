"""Floor amounts to the increments published on a symbol."""

from decimal import Decimal, ROUND_DOWN

from xrocket.exceptions import PrecisionError
from xrocket.models import Symbol


def format_decimal(value: Decimal) -> str:
    """Plain decimal text, without scientific notation."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in {"", "-0"}:
        return "0"
    return text


def floor_to_increment(value: Decimal, increment: Decimal) -> Decimal:
    if increment <= 0:
        raise PrecisionError("increment must be greater than zero")
    steps = (value / increment).to_integral_value(rounding=ROUND_DOWN)
    return steps * increment


def floor_size(size: Decimal, symbol: Symbol) -> Decimal:
    floored = floor_to_increment(size, symbol.base_increment)
    if floored < symbol.base_min_size or floored > symbol.base_max_size:
        raise PrecisionError(
            f"size {format_decimal(floored)} is outside "
            f"{format_decimal(symbol.base_min_size)}..{format_decimal(symbol.base_max_size)}"
        )
    return floored


def floor_price(price: Decimal, symbol: Symbol) -> Decimal:
    floored = floor_to_increment(price, symbol.price_increment)
    if floored < symbol.min_price or floored > symbol.max_price:
        raise PrecisionError(
            f"price {format_decimal(floored)} is outside "
            f"{format_decimal(symbol.min_price)}..{format_decimal(symbol.max_price)}"
        )
    return floored


def quote_step(quote_min_size: Decimal) -> Decimal:
    """Step used when flooring quote ``funds``.

    TODO: a quote/funds increment is NOT DOCUMENTED. The conservative step is
    the decimal scale of ``quoteMinSize`` (``0.01`` -> step ``0.01``).
    """
    if quote_min_size <= 0:
        raise PrecisionError("quoteMinSize must be greater than zero")
    exponent = quote_min_size.normalize().as_tuple().exponent
    if isinstance(exponent, str) or exponent >= 0:
        return Decimal("1")
    return Decimal("1").scaleb(exponent)


def floor_funds(funds: Decimal, symbol: Symbol) -> Decimal:
    floored = floor_to_increment(funds, quote_step(symbol.quote_min_size))
    if floored < symbol.quote_min_size or floored > symbol.quote_max_size:
        raise PrecisionError(
            f"funds {format_decimal(floored)} is outside "
            f"{format_decimal(symbol.quote_min_size)}..{format_decimal(symbol.quote_max_size)}"
        )
    return floored
