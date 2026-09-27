"""CLI: python -m backtest --pair BTC-USDT --days 30

Public candles only. ``--env mainnet`` reads the public mainnet host and
stores candles apart from testnet. No token and no order placement.
"""

import argparse
import asyncio
import csv
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from core.config import Settings, XRocketEnv
from trading.market import Bar, candle_is_closed
from xrocket.exceptions import XRocketError
from xrocket.fee_schedule import FeeSchedule
from xrocket.rest_client import XRocketRestClient

from backtest.data import history_cache_dir, history_rest_url, load_history
from backtest.diagnostics import HoldResult, buy_and_hold, entry_block_counts
from backtest.engine import BacktestResult, run_backtest


def main() -> None:
    args = _parser().parse_args()
    settings = Settings(_env_file=None)
    try:
        code = asyncio.run(_run(args, settings))
    except XRocketError as exc:
        print(f"Биржа вернула ошибку: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    raise SystemExit(code)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m backtest")
    parser.add_argument("--pair", required=True, help="Symbol, for example BTC-USDT")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--timeframe", default="15m")
    parser.add_argument("--equity", default=None, help="Starting equity. Default is PAPER_STARTING_EQUITY.")
    parser.add_argument("--cache", default="data/backtests")
    parser.add_argument("--report", default="data/backtests/reports")
    parser.add_argument(
        "--env",
        choices=("testnet", "mainnet"),
        default="testnet",
        help="Public candle host. Default is testnet. Mainnet is read-only.",
    )
    parser.add_argument(
        "--fee",
        default=None,
        help="Fee fraction for this run only. Omit to read public taker, else 0.003. Does not change the policy default.",
    )
    return parser


async def _run(args: argparse.Namespace, settings: Settings) -> int:
    if args.days < 1:
        print("Число дней должно быть не меньше 1", file=sys.stderr)
        return 2
    symbol_name = str(args.pair).strip().upper()
    env = _as_env(str(args.env))
    rest_url = history_rest_url(env)
    cache_dir = history_cache_dir(Path(args.cache), env)
    client = XRocketRestClient(rest_url, token=None)
    try:
        try:
            fee, fee_source = await _resolve_fee(client, symbol_name, args.fee, settings)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        try:
            rules = await client.get_symbol(symbol_name)
        except XRocketError as exc:
            print(f"Пара {symbol_name} недоступна на {env}: {exc}", file=sys.stderr)
            return 1
    finally:
        await client.close()
    run_settings = settings.model_copy(update={"default_fee_rate": fee})
    bars = await load_history(
        symbol_name,
        args.days,
        rest_url=rest_url,
        cache_dir=cache_dir,
        timeframe=args.timeframe,
    )
    now = datetime.now(timezone.utc)
    closed = [
        bar
        for bar in bars
        if candle_is_closed(bar.open_time, args.timeframe, now, settings.candle_close_grace_seconds)
    ]
    if len(closed) < 2:
        print(f"Для {symbol_name} нет достаточного числа закрытых свечей за {args.days} дн.", file=sys.stderr)
        return 1
    equity = Decimal(str(args.equity)) if args.equity is not None else settings.paper_starting_equity
    result = run_backtest(closed, rules, settings=run_settings, starting_equity=equity)
    hold = buy_and_hold(closed, equity, fee)
    blocks = entry_block_counts(closed, run_settings)
    report_dir = Path(args.report)
    report_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{symbol_name}_{env}_{args.timeframe}_{args.days}d_fee{_fee_label(fee)}"
    json_path = report_dir / f"{stem}.json"
    trades_path = report_dir / f"{stem}_trades.csv"
    equity_path = report_dir / f"{stem}_equity.csv"
    _write_json(
        json_path,
        result,
        bars=len(closed),
        days=args.days,
        env=env,
        rest_url=rest_url,
        fee=fee,
        hold_ending=hold.ending_equity,
        hold_net=hold.net_pnl,
        price_change=hold.price_change,
        blocks=blocks,
        period_start=closed[0].open_time,
        period_end=closed[-1].open_time,
        timeframe=str(args.timeframe),
        fee_source=fee_source,
    )
    _write_trades(trades_path, result)
    _write_equity(equity_path, result)
    print(
        _console(
            result,
            bars=len(closed),
            days=args.days,
            env=env,
            fee=fee,
            fee_source=fee_source,
            timeframe=str(args.timeframe),
            hold=hold,
            blocks=blocks,
            closed=closed,
        )
    )
    print(f"JSON: {json_path}")
    print(f"Сделки CSV: {trades_path}")
    print(f"Кривая CSV: {equity_path}")
    return 0


def _as_env(value: str) -> XRocketEnv:
    match value:
        case "testnet":
            return "testnet"
        case "mainnet":
            return "mainnet"
        case _:
            raise ValueError(f"unsupported env: {value}")


async def _resolve_fee(
    client: XRocketRestClient,
    symbol: str,
    raw: str | None,
    settings: Settings,
) -> tuple[Decimal, str]:
    if raw is not None:
        fee = Decimal(str(raw))
        if fee <= 0 or fee >= 1:
            raise ValueError("Комиссия задаётся долей больше 0 и меньше 1, например 0.003")
        return fee, "override"
    schedule = FeeSchedule(
        refresh_seconds=settings.fee_refresh_seconds,
        treat_as_fraction=settings.fee_rate_is_fraction,
    )
    loaded = await schedule.refresh(client)
    if loaded:
        return schedule.taker_rate(symbol), "trade-fees"
    print(
        "Предупреждение: публичные trade-fees недоступны, для прогона берётся taker 0.003",
        file=sys.stderr,
    )
    return settings.default_fee_rate, "fallback"


def _fee_label(fee: Decimal) -> str:
    return format(fee, "f")


def _console(
    result: BacktestResult,
    *,
    bars: int,
    days: int,
    env: str,
    fee: Decimal,
    fee_source: str,
    timeframe: str,
    hold: HoldResult,
    blocks: dict[str, int],
    closed: list[Bar],
) -> str:
    perf = result.performance
    win = "—" if perf.win_rate is None else f"{(perf.win_rate * Decimal('100')):.2f}%"
    factor = "—" if perf.profit_factor is None else f"{perf.profit_factor:.4f}"
    average_r = "—" if perf.average_r is None else f"{perf.average_r:.4f}"
    ranked = sorted(blocks.items(), key=lambda item: item[1], reverse=True)
    top = ", ".join(f"{name} {count}" for name, count in ranked[:5]) or "—"
    start = closed[0].open_time.isoformat()
    end = closed[-1].open_time.isoformat()
    return "\n".join(
        [
            f"Пара: {result.symbol}",
            f"Источник: {env}",
            f"Таймфрейм: {timeframe}",
            f"Запрошено дней: {days}",
            f"Период: {start} — {end}",
            f"Комиссия прогона: {fee} ({fee_source})",
            f"Закрытых свечей: {bars}",
            f"Сделок: {perf.trades}",
            f"Пропущено из-за издержек: {result.skipped_for_costs}",
            f"Средние издержки на сделку: {_opt_pct(result.average_cost_fraction)}",
            f"Win rate: {win}",
            f"Profit factor: {factor}",
            f"Макс. просадка: {(result.max_drawdown * Decimal('100')):.2f}%",
            f"Текущая просадка: {(result.current_drawdown * Decimal('100')):.2f}%",
            f"Средний R: {average_r}",
            f"Чистый PnL: {perf.net_pnl}",
            f"Комиссии: {perf.fees}",
            f"Старт: {result.starting_equity}",
            f"Капитал на конец (открытая позиция отмечена по последней цене): {result.ending_equity}",
            f"Покупка и удержание, капитал: {hold.ending_equity}",
            f"Покупка и удержание, чистый PnL: {hold.net_pnl}",
            f"Изменение цены за период: {_opt(hold.price_change)}",
            f"Частые фильтры входа: {top}",
        ]
    )


def _write_json(
    path: Path,
    result: BacktestResult,
    *,
    bars: int,
    days: int,
    env: str,
    rest_url: str,
    fee: Decimal,
    hold_ending: Decimal,
    hold_net: Decimal,
    price_change: Decimal | None,
    blocks: dict[str, int],
    period_start: datetime,
    period_end: datetime,
    timeframe: str,
    fee_source: str,
) -> None:
    perf = result.performance
    payload = {
        "symbol": result.symbol,
        "env": env,
        "rest_url": rest_url,
        "fee_rate": _num(fee),
        "fee_source": fee_source,
        "timeframe": timeframe,
        "days": days,
        "skipped_for_costs": result.skipped_for_costs,
        "average_cost_fraction": _opt(result.average_cost_fraction),
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "bars": bars,
        "buy_and_hold_ending": _num(hold_ending),
        "buy_and_hold_net": _num(hold_net),
        "price_change": _opt(price_change),
        "entry_blocks": blocks,
        "starting_equity": _num(result.starting_equity),
        "ending_equity": _num(result.ending_equity),
        "max_drawdown": _num(result.max_drawdown),
        "current_drawdown": _num(result.current_drawdown),
        "trades": perf.trades,
        "wins": perf.wins,
        "losses": perf.losses,
        "win_rate": _opt(perf.win_rate),
        "gross_pnl": _num(perf.gross_pnl),
        "fees": _num(perf.fees),
        "net_pnl": _num(perf.net_pnl),
        "profit_factor": _opt(perf.profit_factor),
        "average_r": _opt(perf.average_r),
        "best": _opt(perf.best),
        "worst": _opt(perf.worst),
        "closed_trades": [
            {
                "entry_time": trade.entry_time.isoformat(),
                "exit_time": trade.exit_time.isoformat(),
                "entry_price": _num(trade.entry_price),
                "exit_price": _num(trade.exit_price),
                "quantity": _num(trade.quantity),
                "net_pnl": _num(trade.net_pnl),
                "fees": _num(trade.fees),
                "entry_fee": _num(trade.entry_fee),
                "exit_fee": _num(trade.exit_fee),
                "entry_spread": _num(trade.entry_spread),
                "exit_spread": _num(trade.exit_spread),
                "entry_book_slippage": _num(trade.entry_book_slippage),
                "exit_book_slippage": _num(trade.exit_book_slippage),
                "entry_paper_slippage": _num(trade.entry_paper_slippage),
                "exit_paper_slippage": _num(trade.exit_paper_slippage),
                "reference_entry": _num(trade.reference_entry),
                "reference_exit": _num(trade.reference_exit),
                "cost_fraction": _opt(trade.cost_fraction),
                "r_multiple": _opt(trade.r_multiple),
                "reason": trade.reason,
            }
            for trade in result.trades
        ],
        "equity_curve": [
            {"time": moment.isoformat(), "equity": _num(value)} for moment, value in result.equity_curve
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_trades(path: Path, result: BacktestResult) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "symbol",
                "entry_time",
                "exit_time",
                "entry_price",
                "exit_price",
                "quantity",
                "gross_pnl",
                "fees",
                "entry_fee",
                "exit_fee",
                "entry_spread",
                "exit_spread",
                "entry_book_slippage",
                "exit_book_slippage",
                "entry_paper_slippage",
                "exit_paper_slippage",
                "net_pnl",
                "cost_fraction",
                "r_multiple",
                "reason",
            ]
        )
        for trade in result.trades:
            writer.writerow(
                [
                    trade.symbol,
                    trade.entry_time.isoformat(),
                    trade.exit_time.isoformat(),
                    _num(trade.entry_price),
                    _num(trade.exit_price),
                    _num(trade.quantity),
                    _num(trade.gross_pnl),
                    _num(trade.fees),
                    _num(trade.entry_fee),
                    _num(trade.exit_fee),
                    _num(trade.entry_spread),
                    _num(trade.exit_spread),
                    _num(trade.entry_book_slippage),
                    _num(trade.exit_book_slippage),
                    _num(trade.entry_paper_slippage),
                    _num(trade.exit_paper_slippage),
                    _num(trade.net_pnl),
                    _opt(trade.cost_fraction),
                    _opt(trade.r_multiple),
                    trade.reason,
                ]
            )


def _write_equity(path: Path, result: BacktestResult) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["time", "equity"])
        for moment, value in result.equity_curve:
            writer.writerow([moment.isoformat(), _num(value)])


def _num(value: Decimal) -> str:
    return format(value, "f")


def _opt(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")


def _opt_pct(value: Decimal | None) -> str:
    if value is None:
        return "—"
    return f"{(value * Decimal('100')):.4f}%"


if __name__ == "__main__":
    main()
