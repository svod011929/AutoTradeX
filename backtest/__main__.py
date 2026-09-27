"""CLI: python -m backtest --pair BTC-USDT --days 30

Public candles, a file cache, a console report, and CSV/JSON files.
No API token and no order placement.
"""

import argparse
import asyncio
import csv
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from core.config import Settings
from trading.market import candle_is_closed
from xrocket.exceptions import XRocketError
from xrocket.rest_client import XRocketRestClient

from backtest.data import load_history
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
    return parser


async def _run(args: argparse.Namespace, settings: Settings) -> int:
    if args.days < 1:
        print("Число дней должно быть не меньше 1", file=sys.stderr)
        return 2
    symbol_name = str(args.pair).strip().upper()
    client = XRocketRestClient(settings.xrocket_rest_url, token=None)
    try:
        rules = await client.get_symbol(symbol_name)
    except XRocketError as exc:
        print(f"Пара {symbol_name} недоступна: {exc}", file=sys.stderr)
        return 1
    finally:
        await client.close()
    bars = await load_history(
        symbol_name,
        args.days,
        rest_url=settings.xrocket_rest_url,
        cache_dir=Path(args.cache),
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
    result = run_backtest(closed, rules, settings=settings, starting_equity=equity)
    report_dir = Path(args.report)
    report_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{symbol_name}_{args.days}d"
    json_path = report_dir / f"{stem}.json"
    trades_path = report_dir / f"{stem}_trades.csv"
    equity_path = report_dir / f"{stem}_equity.csv"
    _write_json(json_path, result, bars=len(closed), days=args.days)
    _write_trades(trades_path, result)
    _write_equity(equity_path, result)
    print(_console(result, bars=len(closed), days=args.days))
    print(f"JSON: {json_path}")
    print(f"Сделки CSV: {trades_path}")
    print(f"Кривая CSV: {equity_path}")
    return 0


def _console(result: BacktestResult, *, bars: int, days: int) -> str:
    perf = result.performance
    win = "—" if perf.win_rate is None else f"{(perf.win_rate * Decimal('100')):.2f}%"
    factor = "—" if perf.profit_factor is None else f"{perf.profit_factor:.4f}"
    average_r = "—" if perf.average_r is None else f"{perf.average_r:.4f}"
    return "\n".join(
        [
            f"Пара: {result.symbol}",
            f"Дней: {days}",
            f"Закрытых свечей: {bars}",
            f"Сделок: {perf.trades}",
            f"Win rate: {win}",
            f"Profit factor: {factor}",
            f"Макс. просадка: {(result.max_drawdown * Decimal('100')):.2f}%",
            f"Текущая просадка: {(result.current_drawdown * Decimal('100')):.2f}%",
            f"Средний R: {average_r}",
            f"Чистый PnL: {perf.net_pnl}",
            f"Комиссии: {perf.fees}",
            f"Старт: {result.starting_equity}",
            f"Капитал на конец (открытая позиция отмечена по последней цене): {result.ending_equity}",
        ]
    )


def _write_json(path: Path, result: BacktestResult, *, bars: int, days: int) -> None:
    perf = result.performance
    payload = {
        "symbol": result.symbol,
        "days": days,
        "bars": bars,
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
                "net_pnl",
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
                    _num(trade.net_pnl),
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


if __name__ == "__main__":
    main()
