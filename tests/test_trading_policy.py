"""Approved stage-3 defaults stay in one place."""

from decimal import Decimal

from core.config import Settings
from core.trading_policy import (
    ATR_HAS_UPPER_BOUND,
    AUTO_TRANSFER_FUNDING_TO_TRADING,
    CANDLE_CLOSE_GRACE_SECONDS,
    CASH_RESERVE_FRACTION,
    DEFAULT_ATR_SL_MULTIPLIER,
    DEFAULT_FEE_RATE,
    DEFAULT_MIN_VOLUME_RATIO,
    DEFAULT_TAKE_PROFIT_RR,
    DEFAULT_TRAILING_ACTIVATION_ATR,
    DEFAULT_TRAILING_ATR_MULTIPLIER,
    MAINNET_CONFIRM_PHRASE,
    MAINNET_DEFAULT_SYMBOLS,
    MARKET_ENTRY_FIELD,
    MARKET_EXIT_FIELD,
    MARKET_TIME_IN_FORCE,
    MAX_SLIPPAGE_FRACTION,
    MAX_SPREAD_FRACTION,
    PAPER_DEFAULT_SYMBOLS,
    PAPER_SLIPPAGE_FRACTION,
    RSI_ENTRY_MAX,
    RSI_ENTRY_MIN,
    TESTNET_DEFAULT_SYMBOLS,
    USE_FULL_ORDERBOOK_SNAPSHOT,
    USE_REST_TRADING_BALANCE,
    VOLUME_MA_PERIOD,
    default_symbols_for_mode,
    exchange_orders_allowed,
    fee_rate_to_fraction,
    generate_client_order_id,
    parse_symbols,
    select_listed_symbols,
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


def test_stage4_exit_candle_fee_and_book_decisions() -> None:
    assert MARKET_EXIT_FIELD == "size"
    assert CANDLE_CLOSE_GRACE_SECONDS == 5
    assert DEFAULT_FEE_RATE == Decimal("0.01")
    assert USE_FULL_ORDERBOOK_SNAPSHOT is True
    assert USE_REST_TRADING_BALANCE is True
    settings = Settings(_env_file=None)
    assert settings.candle_close_grace_seconds == 5
    assert settings.default_fee_rate == Decimal("0.01")
    assert settings.max_spread_fraction == Decimal("0.005")
    assert settings.max_slippage_fraction == Decimal("0.003")
    assert settings.paper_slippage_fraction == Decimal("0.001")
    assert settings.cash_reserve_fraction == Decimal("0.02")
    assert settings.rsi_entry_min == Decimal("30")
    assert settings.rsi_entry_max == Decimal("70")
    assert settings.volume_ma_period == 20
    assert settings.notification_max_attempts == 4
    assert settings.orderbook_depth == 50
    assert settings.paper_starting_equity == Decimal("1000")


def test_stage5_thresholds_and_pair_defaults() -> None:
    assert RSI_ENTRY_MIN == Decimal("30")
    assert RSI_ENTRY_MAX == Decimal("70")
    assert VOLUME_MA_PERIOD == 20
    assert MAX_SPREAD_FRACTION == Decimal("0.005")
    assert MAX_SLIPPAGE_FRACTION == Decimal("0.003")
    assert PAPER_SLIPPAGE_FRACTION == Decimal("0.001")
    assert CASH_RESERVE_FRACTION == Decimal("0.02")
    assert ATR_HAS_UPPER_BOUND is False
    assert PAPER_DEFAULT_SYMBOLS == ("BTC-USDT", "ETH-USDT")
    assert TESTNET_DEFAULT_SYMBOLS == ("BTC-USDT", "ETH-USDT")
    assert MAINNET_DEFAULT_SYMBOLS == ("TON-USDT",)
    assert MAINNET_CONFIRM_PHRASE == "START LIVE"
    assert default_symbols_for_mode("paper") == "BTC-USDT,ETH-USDT"
    assert default_symbols_for_mode("testnet") == "BTC-USDT,ETH-USDT"
    assert default_symbols_for_mode("mainnet") == "TON-USDT"
    paper = Settings(_env_file=None, execution_mode="paper", default_symbols="")
    assert paper.resolved_symbols() == "BTC-USDT,ETH-USDT"
    explicit = Settings(_env_file=None, execution_mode="paper", default_symbols="SOL-USDT")
    assert explicit.resolved_symbols() == "SOL-USDT"
    assert parse_symbols(" btc-usdt, BTC-USDT, eth-usdt ") == ["BTC-USDT", "ETH-USDT"]

    listed = {"BTC-USDT", "ETH-USDT"}
    dropped = select_listed_symbols(["TON-USDT", "BTC-USDT"], listed, mode="paper")
    assert dropped.kept == ("BTC-USDT",)
    assert dropped.dropped == ("TON-USDT",)
    assert dropped.refusal is None
    assert any("TON-USDT" in line for line in dropped.warnings)

    empty = select_listed_symbols(["TON-USDT"], listed, mode="paper")
    assert empty.kept == ()
    assert empty.refusal is not None
    assert "Запуск отменён" in empty.refusal

    mainnet = select_listed_symbols(["TON-USDT", "BTC-USDT"], listed, mode="mainnet")
    assert mainnet.kept == ("BTC-USDT",)
    assert mainnet.refusal is not None
    assert "TON-USDT" in mainnet.refusal

    mainnet_ok = select_listed_symbols(["BTC-USDT"], {"BTC-USDT", "TON-USDT"}, mode="mainnet")
    assert mainnet_ok.kept == ("BTC-USDT",)
    assert mainnet_ok.refusal is None
