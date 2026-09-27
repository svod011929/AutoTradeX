"""Approved stage-3 defaults stay in one place."""

from decimal import Decimal

from core.config import Settings
from core.trading_policy import (
    AUTO_TRANSFER_FUNDING_TO_TRADING,
    DEFAULT_ATR_SL_MULTIPLIER,
    DEFAULT_MIN_VOLUME_RATIO,
    DEFAULT_TAKE_PROFIT_RR,
    DEFAULT_TRAILING_ACTIVATION_ATR,
    DEFAULT_TRAILING_ATR_MULTIPLIER,
    MARKET_ENTRY_FIELD,
    MARKET_TIME_IN_FORCE,
    exchange_orders_allowed,
    fee_rate_to_fraction,
    generate_client_order_id,
    trading_balance_warning,
    validate_client_order_id,
)
from database.models import StrategySettings


def test_approved_numbers_and_entry_shape() -> None:
    assert DEFAULT_ATR_SL_MULTIPLIER == Decimal("2.0")
    assert DEFAULT_TAKE_PROFIT_RR == Decimal("2.0")
    assert DEFAULT_TRAILING_ACTIVATION_ATR == Decimal("1.5")
    assert DEFAULT_TRAILING_ATR_MULTIPLIER == Decimal("1.5")
    assert DEFAULT_MIN_VOLUME_RATIO == Decimal("1.0")
    assert MARKET_TIME_IN_FORCE == "IOC"
    assert MARKET_ENTRY_FIELD == "funds"
    assert AUTO_TRANSFER_FUNDING_TO_TRADING is False


def test_paper_blocks_orders_even_when_the_flag_is_on() -> None:
    assert exchange_orders_allowed("paper", True) is False
    assert exchange_orders_allowed("testnet", False) is False
    assert exchange_orders_allowed("testnet", True) is True
    assert exchange_orders_allowed("mainnet", True) is True
    settings = Settings(_env_file=None, execution_mode="paper", allow_exchange_orders=True)
    assert settings.exchange_orders_allowed() is False
    enabled = Settings(_env_file=None, execution_mode="testnet", allow_exchange_orders=True)
    assert enabled.exchange_orders_allowed() is True


def test_fee_default_treats_rate_as_a_fraction() -> None:
    assert fee_rate_to_fraction("0.01", True) == Decimal("0.01")
    assert fee_rate_to_fraction("0.01", False) == Decimal("0.0001")
    settings = Settings(_env_file=None)
    assert settings.fee_rate_is_fraction is True
    assert settings.atr_sl_multiplier == Decimal("2.0")
    assert settings.take_profit_rr == Decimal("2.0")


def test_client_order_id_is_legal_and_warning_does_not_transfer() -> None:
    client_order_id = generate_client_order_id()
    assert validate_client_order_id(client_order_id) == client_order_id
    warning = trading_balance_warning("USDT", "1", "5")
    assert "USDT" in warning
    assert "does not transfer" in warning


def test_strategy_columns_use_the_approved_defaults() -> None:
    columns = StrategySettings.__table__.c
    assert columns.atr_sl_multiplier.default.arg == DEFAULT_ATR_SL_MULTIPLIER
    assert columns.take_profit_rr.default.arg == DEFAULT_TAKE_PROFIT_RR
    assert columns.trailing_activation_atr.default.arg == DEFAULT_TRAILING_ACTIVATION_ATR
    assert columns.trailing_atr_multiplier.default.arg == DEFAULT_TRAILING_ATR_MULTIPLIER
    assert columns.min_volume_ratio.default.arg == DEFAULT_MIN_VOLUME_RATIO
    assert str(columns.atr_sl_multiplier.server_default.arg) == "2.0"
    assert str(columns.min_volume_ratio.server_default.arg) == "1.0"
