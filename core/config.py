"""Runtime settings loaded from the environment."""

from decimal import Decimal
from functools import lru_cache
from typing import Literal, Never

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.trading_policy import (
    ALLOW_EXCHANGE_ORDERS_DEFAULT,
    CANDLE_CLOSE_GRACE_SECONDS,
    CASH_RESERVE_FRACTION,
    DEFAULT_ATR_SL_MULTIPLIER,
    BACKTEST_ASSUMED_SPREAD_FRACTION,
    DEFAULT_FEE_RATE,
    DEFAULT_MIN_VOLUME_RATIO,
    FEE_REFRESH_SECONDS,
    MIN_NET_REWARD_RISK,
    DEFAULT_TAKE_PROFIT_RR,
    DEFAULT_TRAILING_ACTIVATION_ATR,
    DEFAULT_TRAILING_ATR_MULTIPLIER,
    EXECUTION_MODE_DEFAULT,
    FEE_RATE_IS_FRACTION_DEFAULT,
    MAX_SLIPPAGE_FRACTION,
    MAX_SPREAD_FRACTION,
    PAPER_SLIPPAGE_FRACTION,
    RSI_ENTRY_MAX,
    RSI_ENTRY_MIN,
    VOLUME_MA_PERIOD,
    ExecutionModeName,
    default_symbols_for_mode,
    exchange_orders_allowed as orders_allowed_for_mode,
)

XRocketEnv = Literal["testnet", "mainnet"]
RiskName = Literal["low", "medium", "high"]


class RiskProfileLimits:
    """Per-trade and daily loss are fractions of equity (0.01 == 1%)."""

    def __init__(self, risk_per_trade: Decimal, daily_loss: Decimal, max_positions: int) -> None:
        self.risk_per_trade = risk_per_trade
        self.daily_loss = daily_loss
        self.max_positions = max_positions


# Numbers come from the client spec, not from the exchange.
RISK_PROFILES: dict[RiskName, RiskProfileLimits] = {
    "low": RiskProfileLimits(Decimal("0.005"), Decimal("0.02"), 1),
    "medium": RiskProfileLimits(Decimal("0.01"), Decimal("0.03"), 2),
    "high": RiskProfileLimits(Decimal("0.02"), Decimal("0.05"), 3),
}


def _assert_never(value: Never) -> Never:
    raise AssertionError(f"unhandled value: {value}")


def default_rest_url(env: XRocketEnv) -> str:
    match env:
        case "testnet":
            return "https://exchange.api.testnet.xrocket.exchange"
        case "mainnet":
            return "https://exchange.api.xrocket.exchange"
        case _ as unreachable:
            _assert_never(unreachable)


def default_ws_url(env: XRocketEnv) -> str:
    match env:
        case "testnet":
            return "wss://exchange.app-api.testnet.xrocket.exchange/"
        case "mainnet":
            return "wss://exchange.app-api.xrocket.exchange/"
        case _ as unreachable:
            _assert_never(unreachable)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    bot_token: SecretStr = SecretStr("")
    admin_ids: str = ""
    xrocket_env: XRocketEnv = "testnet"
    xrocket_rest_url: str = ""
    xrocket_ws_url: str = ""
    database_url: str = "sqlite+aiosqlite:///./data/autotrade.db"
    encryption_key: SecretStr = SecretStr("")
    log_level: str = "INFO"
    default_timeframe: str = "15m"
    default_risk: RiskName = "medium"
    max_retries: int = 3
    ws_ping_interval: int = 30
    sqlite_busy_timeout: int = 5000
    sqlite_wal_autocheckpoint: int = 1000
    backup_interval_hours: int = 24
    backup_retention: int = 7
    cooldown_minutes: int = 30
    execution_mode: ExecutionModeName = EXECUTION_MODE_DEFAULT
    allow_exchange_orders: bool = ALLOW_EXCHANGE_ORDERS_DEFAULT
    fee_rate_is_fraction: bool = FEE_RATE_IS_FRACTION_DEFAULT
    rest_timeout_seconds: float = 10
    rest_max_rps: float = 2
    ws_stale_seconds: float = 90
    ws_reconnect_base_seconds: float = 1
    ws_reconnect_max_seconds: float = 30
    atr_sl_multiplier: Decimal = DEFAULT_ATR_SL_MULTIPLIER
    trailing_activation_atr: Decimal = DEFAULT_TRAILING_ACTIVATION_ATR
    trailing_atr_multiplier: Decimal = DEFAULT_TRAILING_ATR_MULTIPLIER
    min_volume_ratio: Decimal = DEFAULT_MIN_VOLUME_RATIO
    take_profit_rr: Decimal = DEFAULT_TAKE_PROFIT_RR
    candle_close_grace_seconds: int = CANDLE_CLOSE_GRACE_SECONDS
    default_fee_rate: Decimal = DEFAULT_FEE_RATE
    fee_refresh_seconds: int = FEE_REFRESH_SECONDS
    min_net_reward_risk: Decimal = MIN_NET_REWARD_RISK
    backtest_assumed_spread_fraction: Decimal = BACKTEST_ASSUMED_SPREAD_FRACTION
    orderbook_depth: int = 50
    max_spread_fraction: Decimal = MAX_SPREAD_FRACTION
    max_slippage_fraction: Decimal = MAX_SLIPPAGE_FRACTION
    paper_slippage_fraction: Decimal = PAPER_SLIPPAGE_FRACTION
    cash_reserve_fraction: Decimal = CASH_RESERVE_FRACTION
    rsi_entry_min: Decimal = RSI_ENTRY_MIN
    rsi_entry_max: Decimal = RSI_ENTRY_MAX
    volume_ma_period: int = VOLUME_MA_PERIOD
    notification_max_attempts: int = 4
    paper_starting_equity: Decimal = Decimal("1000")
    default_symbols: str = ""

    @field_validator("log_level")
    @classmethod
    def _normalize_log_level(cls, value: str) -> str:
        normalized = value.strip().upper()
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if normalized not in allowed:
            raise ValueError(f"LOG_LEVEL must be one of {sorted(allowed)}")
        return normalized

    @field_validator("sqlite_busy_timeout", "sqlite_wal_autocheckpoint", "max_retries", "cooldown_minutes")
    @classmethod
    def _non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("value must be >= 0")
        return value

    @field_validator(
        "backup_retention",
        "backup_interval_hours",
        "ws_ping_interval",
        "volume_ma_period",
        "fee_refresh_seconds",
    )
    @classmethod
    def _positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("value must be >= 1")
        return value

    @field_validator("candle_close_grace_seconds")
    @classmethod
    def _grace(cls, value: int) -> int:
        if value < 0:
            raise ValueError("grace must be >= 0")
        return value

    @field_validator("notification_max_attempts")
    @classmethod
    def _attempts(cls, value: int) -> int:
        if value < 3 or value > 5:
            raise ValueError("notification attempts must be from 3 to 5")
        return value

    @field_validator("orderbook_depth")
    @classmethod
    def _depth(cls, value: int) -> int:
        if value not in {5, 10, 20, 50, 100, 200, 500}:
            raise ValueError("orderbook depth must be one of 5, 10, 20, 50, 100, 200, 500")
        return value

    @field_validator(
        "rest_timeout_seconds",
        "rest_max_rps",
        "ws_stale_seconds",
        "ws_reconnect_base_seconds",
        "ws_reconnect_max_seconds",
    )
    @classmethod
    def _positive_float(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("value must be > 0")
        return value

    @field_validator(
        "atr_sl_multiplier",
        "trailing_activation_atr",
        "trailing_atr_multiplier",
        "min_volume_ratio",
        "take_profit_rr",
        "default_fee_rate",
        "paper_starting_equity",
    )
    @classmethod
    def _positive_decimal(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("value must be > 0")
        return value

    @field_validator(
        "max_spread_fraction",
        "max_slippage_fraction",
        "paper_slippage_fraction",
        "cash_reserve_fraction",
        "backtest_assumed_spread_fraction",
    )
    @classmethod
    def _fraction(cls, value: Decimal) -> Decimal:
        if value < 0 or value >= 1:
            raise ValueError("fraction must be >= 0 and < 1")
        return value

    @field_validator("min_net_reward_risk")
    @classmethod
    def _min_net_reward_risk(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("min net reward/risk must be >= 0")
        return value

    @model_validator(mode="after")
    def _fill_exchange_urls(self) -> "Settings":
        if self.xrocket_rest_url.strip() == "":
            self.xrocket_rest_url = default_rest_url(self.xrocket_env)
        if self.xrocket_ws_url.strip() == "":
            self.xrocket_ws_url = default_ws_url(self.xrocket_env)
        return self

    def exchange_orders_allowed(self) -> bool:
        return orders_allowed_for_mode(self.execution_mode, self.allow_exchange_orders)

    def resolved_symbols(self) -> str:
        """Explicit DEFAULT_SYMBOLS wins. An empty value follows the execution mode."""
        raw = self.default_symbols.strip()
        if raw == "":
            return default_symbols_for_mode(self.execution_mode)
        return raw

    @property
    def admin_id_list(self) -> list[int]:
        if self.admin_ids.strip() == "":
            return []
        return [int(part.strip()) for part in self.admin_ids.split(",") if part.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
