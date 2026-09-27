"""Pydantic models for the documented xRocket payloads.

Amounts stay Decimal. Candle rows follow the documented order
``[start, open, close, high, low, baseVolume]``.
"""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

OrderSide = Literal["buy", "sell"]
OrderTypeName = Literal["limit", "market", "stopLimit", "stopMarket"]


def _as_decimal(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool) or value is None:
        raise ValueError("expected a decimal amount")
    return Decimal(str(value))


def _as_optional_decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    return _as_decimal(value)


def _as_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    return datetime.fromisoformat(text)


def _as_optional_datetime(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    return _as_datetime(value)


Money = Annotated[Decimal, BeforeValidator(_as_decimal)]
OptionalMoney = Annotated[Decimal | None, BeforeValidator(_as_optional_decimal)]
Timestamp = Annotated[datetime, BeforeValidator(_as_datetime)]
OptionalTimestamp = Annotated[datetime | None, BeforeValidator(_as_optional_datetime)]


class Symbol(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    symbol: str
    base_asset: str = Field(alias="baseAsset")
    quote_asset: str = Field(alias="quoteAsset")
    base_min_size: Money = Field(alias="baseMinSize")
    quote_min_size: Money = Field(alias="quoteMinSize")
    base_max_size: Money = Field(alias="baseMaxSize")
    quote_max_size: Money = Field(alias="quoteMaxSize")
    min_price: Money = Field(alias="minPrice")
    max_price: Money = Field(alias="maxPrice")
    base_increment: Money = Field(alias="baseIncrement")
    price_increment: Money = Field(alias="priceIncrement")
    enable_trading: bool = Field(alias="enableTrading")
    open_time: OptionalTimestamp = Field(default=None, alias="openTime")
    precisions: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)


class Ticker(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    symbol: str
    start_time: Timestamp = Field(alias="startTime")
    end_time: Timestamp = Field(alias="endTime")
    open: Money
    close: Money
    high: Money
    low: Money
    change_rate: Money = Field(alias="changeRate")
    change_price: Money = Field(alias="changePrice")
    base_volume: Money = Field(alias="baseVolume")
    quote_volume: Money = Field(alias="quoteVolume")
    last: Money


class Candle(BaseModel):
    model_config = ConfigDict(extra="ignore")

    start: Timestamp
    open: Money
    close: Money
    high: Money
    low: Money
    base_volume: Money

    @classmethod
    def from_array(cls, row: list[object] | tuple[object, ...]) -> "Candle":
        if len(row) != 6:
            raise ValueError("candle must be [start, open, close, high, low, baseVolume]")
        start, open_, close, high, low, base_volume = row
        return cls(
            start=start,  # type: ignore[arg-type]
            open=open_,  # type: ignore[arg-type]
            close=close,  # type: ignore[arg-type]
            high=high,  # type: ignore[arg-type]
            low=low,  # type: ignore[arg-type]
            base_volume=base_volume,  # type: ignore[arg-type]
        )


class BookLevel(BaseModel):
    price: Money
    size: Money

    @classmethod
    def from_pair(cls, pair: list[object] | tuple[object, ...]) -> "BookLevel":
        if len(pair) != 2:
            raise ValueError("book level must be [price, size]")
        return cls(price=pair[0], size=pair[1])  # type: ignore[arg-type]


class OrderBook(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    sequence: str
    bids: list[BookLevel]
    asks: list[BookLevel]
    ask_total_amount: Money = Field(alias="askTotalAmount")
    bid_total_amount: Money = Field(alias="bidTotalAmount")
    snapshot: bool | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> "OrderBook":
        bids = payload.get("bids") or []
        asks = payload.get("asks") or []
        return cls(
            sequence=str(payload.get("sequence", "")),
            bids=[BookLevel.from_pair(pair) for pair in bids],  # type: ignore[arg-type]
            asks=[BookLevel.from_pair(pair) for pair in asks],  # type: ignore[arg-type]
            askTotalAmount=payload.get("askTotalAmount", "0"),  # type: ignore[arg-type]
            bidTotalAmount=payload.get("bidTotalAmount", "0"),  # type: ignore[arg-type]
            snapshot=payload.get("snapshot") if isinstance(payload.get("snapshot"), bool) else None,
        )


class StandardFee(BaseModel):
    model_config = ConfigDict(extra="ignore")

    taker: Money
    maker: Money


class TradeFee(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    symbol: str
    standard: StandardFee


class AssetBalance(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    asset: str
    balance: Money
    available: Money
    holds: Money


class ExchangeOrder(BaseModel):
    """One order object. ``average_price`` is derived, not an API field."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str
    client_order_id: str | None = Field(default=None, alias="clientOrderId")
    symbol: str
    side: OrderSide
    status: str
    created_at: Timestamp = Field(alias="createdAt")
    updated_at: Timestamp = Field(alias="updatedAt")
    deal_size: Money = Field(alias="dealSize")
    deal_funds: Money = Field(alias="dealFunds")
    fee: Money
    fee_asset: str = Field(alias="feeAsset")
    remark: str | None = None
    time_in_force: str = Field(alias="timeInForce")
    type: str
    size: OptionalMoney = None
    funds: OptionalMoney = None
    price: OptionalMoney = None
    stop_price: OptionalMoney = Field(default=None, alias="stopPrice")
    stop_triggered: bool | None = Field(default=None, alias="stopTriggered")

    @property
    def filled_quantity(self) -> Decimal:
        return self.deal_size

    @property
    def average_price(self) -> Decimal | None:
        if self.deal_size == 0:
            return None
        return self.deal_funds / self.deal_size


class OrderHistoryPage(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    orders: list[ExchangeOrder]
    current_page: int = Field(alias="currentPage")
    page_size: int = Field(alias="pageSize")
    total_num: int = Field(alias="totalNum")
    total_page: int = Field(alias="totalPage")


class CancelResult(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    cancelled_order_ids: list[str] = Field(alias="cancelledOrderIds")


class PublicTrade(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    trade_id: str = Field(alias="tradeId")
    price: Money
    side: OrderSide
    time: Timestamp
    size: Money
