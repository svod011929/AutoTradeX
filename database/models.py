"""SQLite schema for AutoTrade X.

Column set follows the client summary: every named table, the named fields,
and the named indexes. Exchange order statuses match the xRocket API.
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from core.trading_policy import (
    DEFAULT_ATR_SL_MULTIPLIER,
    DEFAULT_MIN_VOLUME_RATIO,
    DEFAULT_TAKE_PROFIT_RR,
    DEFAULT_TRAILING_ACTIVATION_ATR,
    DEFAULT_TRAILING_ATR_MULTIPLIER,
)

MONEY = Numeric(36, 18)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class ExecutionMode(StrEnum):
    PAPER = "paper"
    TESTNET = "testnet"
    MAINNET = "mainnet"


class PositionStatus(StrEnum):
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"


class OrderStatus(StrEnum):
    PENDING_SUBMIT = "pending_submit"
    UNKNOWN = "unknown"
    WORKING = "working"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    EXPIRED = "expired"
    PENDING = "pending"
    SENDING = "sending"


class NotificationStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class User(Base):
    __tablename__ = "users"
    __table_args__ = (Index("ix_users_telegram_id", "telegram_id", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class XRocketAccount(Base):
    __tablename__ = "xrocket_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    encrypted_api_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    environment: Mapped[str] = mapped_column(String(16), nullable=False)
    is_connected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_balance_sync: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_ws_sync: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class Strategy(Base):
    __tablename__ = "strategies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), default="trend_following", nullable=False)
    timeframe: Mapped[str] = mapped_column(String(16), default="15m", nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class StrategySettings(Base):
    __tablename__ = "strategy_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    strategy_id: Mapped[int] = mapped_column(
        ForeignKey("strategies.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    ema_fast: Mapped[int] = mapped_column(Integer, default=20, nullable=False)
    ema_slow: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    rsi_period: Mapped[int] = mapped_column(Integer, default=14, nullable=False)
    atr_period: Mapped[int] = mapped_column(Integer, default=14, nullable=False)
    atr_sl_multiplier: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), default=DEFAULT_ATR_SL_MULTIPLIER, server_default="2.0", nullable=False
    )
    take_profit_rr: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), default=DEFAULT_TAKE_PROFIT_RR, server_default="2.0", nullable=False
    )
    trailing_activation_atr: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), default=DEFAULT_TRAILING_ACTIVATION_ATR, server_default="1.5", nullable=False
    )
    trailing_atr_multiplier: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), default=DEFAULT_TRAILING_ATR_MULTIPLIER, server_default="1.5", nullable=False
    )
    min_volume_ratio: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), default=DEFAULT_MIN_VOLUME_RATIO, server_default="1.0", nullable=False
    )
    cooldown_minutes: Mapped[int] = mapped_column(Integer, default=30, nullable=False)


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (
        Index("ix_positions_user_id_status", "user_id", "status"),
        Index("ix_positions_symbol_status", "symbol", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    strategy_id: Mapped[int | None] = mapped_column(ForeignKey("strategies.id", ondelete="SET NULL"), nullable=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), default="long", nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    entry_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    quantity: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    stop_loss: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    take_profit: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    trailing_stop: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    peak_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_orders_xrocket_order_id", "xrocket_order_id"),
        Index("ix_orders_client_order_id", "client_order_id", unique=True),
        Index("ix_orders_user_id_status", "user_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    position_id: Mapped[int | None] = mapped_column(ForeignKey("positions.id", ondelete="SET NULL"), nullable=True)
    client_order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    xrocket_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    order_type: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    quantity: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    filled_quantity: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"), nullable=False)
    average_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    fee: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    fee_asset: Mapped[str | None] = mapped_column(String(16), nullable=True)
    time_in_force: Mapped[str | None] = mapped_column(String(8), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class TradeSignal(Base):
    __tablename__ = "trade_signals"
    __table_args__ = (Index("ix_trade_signals_user_id_created_at", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    strategy_id: Mapped[int | None] = mapped_column(ForeignKey("strategies.id", ondelete="SET NULL"), nullable=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class Trade(Base):
    __tablename__ = "trades"
    __table_args__ = (Index("ix_trades_user_id_closed_at", "user_id", "closed_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    position_id: Mapped[int | None] = mapped_column(ForeignKey("positions.id", ondelete="SET NULL"), nullable=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"), nullable=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    entry_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    exit_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    gross_pnl: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    fees: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    net_pnl: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DailyStat(Base):
    __tablename__ = "daily_stats"
    __table_args__ = (
        UniqueConstraint("user_id", "date", name="uq_daily_stats_user_id_date"),
        Index("ix_daily_stats_user_id_date", "user_id", "date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    trades_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    wins: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    losses: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    gross_pnl: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"), nullable=False)
    fees: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"), nullable=False)
    net_pnl: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"), nullable=False)


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_user_id_sent", "user_id", "sent"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=NotificationStatus.PENDING.value, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class BotSettings(Base):
    __tablename__ = "bot_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(16), default=ExecutionMode.PAPER.value, nullable=False)
    risk_profile: Mapped[str] = mapped_column(String(16), default="medium", nullable=False)
    enabled_symbols: Mapped[str] = mapped_column(Text, default="TON-USDT,BTC-USDT", nullable=False)
    timeframe: Mapped[str] = mapped_column(String(16), default="15m", nullable=False)
    is_paused: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    emergency_stop: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    live_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class SystemEvent(Base):
    __tablename__ = "system_events"
    __table_args__ = (Index("ix_system_events_created_at", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    event_metadata: Mapped[str | None] = mapped_column("metadata", Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class Heartbeat(Base):
    __tablename__ = "heartbeats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    service: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Candle(Base):
    __tablename__ = "candles"
    __table_args__ = (
        Index(
            "uq_candles_symbol_timeframe_open_time",
            "symbol",
            "timeframe",
            "open_time",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False)
    open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    high: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    low: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    close: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    volume: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
