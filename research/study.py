"""In-sample search, one blind out-of-sample run, and a yearly walk-forward.

The last 365 days are never used to pick parameters. Walk-forward windows
also stop at that cut.
"""

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from backtest.data import history_cache_dir, history_rest_url, load_history
from backtest.diagnostics import buy_and_hold
from backtest.metrics import equity_drawdown
from trading.indicators import atr
from trading.market import Bar

from research.binance_data import download, load_cached
from research.costs import TAKER_FEE
from research.engine import STARTING_EQUITY, ResearchResult, _cagr, run_strategy
from research.grid import GridParams, run_grid
from research.strategies.base import TimeframeName
from research.strategies.donchian import DonchianStrategy
from research.strategies.regime import RegimeStrategy

REPORT_PATH = Path("data/research/summary.json")
DOC_PATH = Path("docs/RESEARCH_ENTRIES.md")
MIN_TRADES = 10
OOS_DAYS = 365
XROCKET_DAYS = 180
PAIRS = ("BTCUSDT", "ETHUSDT")
TIMEFRAMES: tuple[TimeframeName, ...] = ("1d", "4h", "1h")


@dataclass(frozen=True)
class Job:
    variant: str
    pair: str
    timeframe: TimeframeName
    params: dict[str, str]
    kind: str


def run_study() -> None:
    series = _load_series()
    atr_cache = {(pair, tf): _align_atr(bars) for (pair, tf), bars in series.items()}
    cuts = {key: _cut_index(bars) for key, bars in series.items()}
    is_rows: list[dict[str, object]] = []
    oos_rows: list[dict[str, object]] = []
    wf_rows: list[dict[str, object]] = []
    chosen: list[dict[str, object]] = []
    counts: dict[str, int] = {}

    regime_grid = [{"ma_days": str(ma), "slope_days": str(slope)} for ma in (100, 150, 200) for slope in (10, 20, 40)]
    donchian_grid = [
        {"channel": str(channel), "atr_mult": str(mult), "use_regime": "0"}
        for channel in (20, 55, 100)
        for mult in (2, 3, 4)
    ]
    donchian_regime_grid = [
        {"channel": str(channel), "atr_mult": str(mult), "use_regime": "1"}
        for channel in (20, 55, 100)
        for mult in (2, 3, 4)
    ]
    grid_grid = [
        {"step": step, "max_levels": str(levels), "lookback_days": str(lookback)}
        for step in ("0.005", "0.008", "0.012")
        for levels in (4, 6, 8)
        for lookback in (30, 60)
    ]
    maker_grid = [{"offset_atr": offset, "timeout": str(timeout)} for offset in ("0.5", "1") for timeout in (4, 8)]

    families: list[tuple[str, tuple[TimeframeName, ...], list[dict[str, str]]]] = [
        ("regime", TIMEFRAMES, regime_grid),
        ("donchian", ("4h", "1d"), donchian_grid),
        ("donchian_regime", ("4h", "1d"), donchian_regime_grid),
        ("grid", ("4h", "1d"), grid_grid),
    ]
    frozen: dict[tuple[str, str, str], dict[str, str]] = {}
    maker_frozen: dict[tuple[str, str, str], dict[str, str]] = {}
    for variant, frames, grid in families:
        counts[variant] = len(grid) * len(PAIRS) * len(frames)
        for pair in PAIRS:
            for timeframe in frames:
                key = (pair, timeframe)
                bars = series[key]
                cut = cuts[key]
                scored: list[dict[str, object]] = []
                for params in grid:
                    result = _run(bars, variant, timeframe, params, 0, cut, "market", atr_cache[key])
                    row = _row(variant, pair, timeframe, params, "market", result, _benchmark(bars, 0, cut), "is")
                    scored.append(row)
                    is_rows.append(row)
                best, trusted = _choose(scored)
                frozen[(variant, pair, timeframe)] = dict(best["params"])  # type: ignore[arg-type]
                chosen.append({**best, "trusted": trusted, "combinations": len(grid)})
                oos = _run(bars, variant, timeframe, frozen[(variant, pair, timeframe)], cut, len(bars), "market", atr_cache[key])
                oos_rows.append(
                    _row(variant, pair, timeframe, frozen[(variant, pair, timeframe)], "market", oos, _benchmark(bars, cut, len(bars)), "oos")
                )
                wf_rows.append(_walk_forward(bars, variant, timeframe, pair, grid, cut, atr_cache[key]))
                print(f"frozen {variant} {pair} {timeframe} trusted={trusted} trades={best['trades']}", flush=True)

    maker_jobs = [(key, params) for key, params in frozen.items() if key[0] != "grid"]
    counts["maker"] = len(maker_grid) * len(maker_jobs)
    for (variant, pair, timeframe), params in maker_jobs:
        bars = series[(pair, timeframe)]
        cut = cuts[(pair, timeframe)]
        scored = []
        for maker in maker_grid:
            merged = {**params, **maker}
            result = _run(
                bars,
                variant,
                timeframe,
                merged,
                0,
                cut,
                "maker",
                atr_cache[(pair, timeframe)],
            )
            row = _row(f"{variant}_maker", pair, timeframe, merged, "maker", result, _benchmark(bars, 0, cut), "is")
            scored.append(row)
            is_rows.append(row)
        best, trusted = _choose(scored)
        maker_params = dict(best["params"])  # type: ignore[arg-type]
        maker_frozen[(variant, pair, timeframe)] = maker_params
        chosen.append({**best, "trusted": trusted, "combinations": len(maker_grid)})
        oos = _run(bars, variant, timeframe, maker_params, cut, len(bars), "maker", atr_cache[(pair, timeframe)])
        oos_rows.append(
            _row(f"{variant}_maker", pair, timeframe, maker_params, "maker", oos, _benchmark(bars, cut, len(bars)), "oos")
        )
        print(f"frozen maker {variant} {pair} {timeframe} fill={best['fill_rate']}", flush=True)

    xrocket_rows = _xrocket(frozen, maker_frozen)
    payload = {
        "combinations": counts,
        "min_trades": MIN_TRADES,
        "oos_days": OOS_DAYS,
        "chosen": _jsonable(chosen),
        "in_sample": _jsonable(is_rows),
        "out_of_sample": _jsonable(oos_rows),
        "walk_forward": _jsonable(wf_rows),
        "xrocket": _jsonable(xrocket_rows),
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    DOC_PATH.write_text(_markdown(payload), encoding="utf-8")
    print(f"wrote {DOC_PATH}", flush=True)


def _load_series() -> dict[tuple[str, str], list[Bar]]:
    loaded: dict[tuple[str, str], list[Bar]] = {}
    for pair in PAIRS:
        for timeframe in TIMEFRAMES:
            try:
                bars = load_cached(pair, timeframe)
            except FileNotFoundError:
                bars = []
            if len(bars) < 1000:
                print(f"download {pair} {timeframe}", flush=True)
                bars = download(pair, timeframe)
            loaded[(pair, timeframe)] = bars
            print(f"loaded {pair} {timeframe} {len(bars)} {bars[0].open_time.date()} {bars[-1].open_time.date()}", flush=True)
    return loaded


def _cut_index(bars: list[Bar]) -> int:
    cut = bars[-1].open_time - timedelta(days=OOS_DAYS)
    for index, bar in enumerate(bars):
        if bar.open_time >= cut:
            return index
    return len(bars) - 1


def _align_atr(bars: list[Bar], period: int = 14) -> list[Decimal | None]:
    raw = atr([bar.high for bar in bars], [bar.low for bar in bars], [bar.close for bar in bars], period)
    aligned: list[Decimal | None] = [None] * len(bars)
    for offset, value in enumerate(raw):
        aligned[offset + period - 1] = value
    return aligned


def _run(
    bars: list[Bar],
    variant: str,
    timeframe: TimeframeName,
    params: dict[str, str],
    start: int,
    end: int,
    execution: str,
    atr_at: list[Decimal | None],
) -> ResearchResult:
    if variant == "grid":
        return run_grid(
            bars,
            GridParams(
                timeframe=timeframe,
                step=Decimal(params["step"]),
                max_levels=int(params["max_levels"]),
                lookback_days=int(params["lookback_days"]),
            ),
            start=start,
            end=end,
        )
    strategy: RegimeStrategy | DonchianStrategy
    if variant == "regime":
        strategy = RegimeStrategy(
            timeframe=timeframe,
            ma_days=int(params["ma_days"]),
            slope_days=int(params["slope_days"]),
        )
    else:
        strategy = DonchianStrategy(
            timeframe=timeframe,
            channel=int(params["channel"]),
            atr_mult=Decimal(params["atr_mult"]),
            use_regime=params.get("use_regime") == "1",
        )
    return run_strategy(
        bars,
        strategy,
        start=start,
        end=end,
        execution=execution,
        offset_atr=Decimal(params.get("offset_atr", "0.5")),
        timeout=int(params.get("timeout", "4")),
        atr_at=atr_at,
    )


def _benchmark(bars: list[Bar], start: int, end: int) -> dict[str, Decimal | None]:
    segment = bars[start:end]
    if len(segment) < 2:
        return {"ending": STARTING_EQUITY, "net_return": Decimal("0"), "cagr": None, "max_drawdown": Decimal("0"), "ratio": None}
    hold = buy_and_hold(segment, STARTING_EQUITY, TAKER_FEE)
    first = segment[0].close
    quantity = STARTING_EQUITY / (first * (Decimal("1") + TAKER_FEE))
    curve = [quantity * bar.close for bar in segment]
    curve[-1] = hold.ending_equity
    max_dd, _current = equity_drawdown(curve)
    span = Decimal(str((segment[-1].open_time - segment[0].open_time).total_seconds())) / Decimal("86400")
    net_return = hold.ending_equity / STARTING_EQUITY - Decimal("1")
    cagr = _cagr(STARTING_EQUITY, hold.ending_equity, span)
    ratio = None if max_dd <= 0 else net_return / max_dd
    return {"ending": hold.ending_equity, "net_return": net_return, "cagr": cagr, "max_drawdown": max_dd, "ratio": ratio}


def _row(
    variant: str,
    pair: str,
    timeframe: str,
    params: dict[str, str],
    execution: str,
    result: ResearchResult,
    hold: dict[str, Decimal | None],
    sample: str,
) -> dict[str, object]:
    return {
        "sample": sample,
        "variant": variant,
        "pair": pair,
        "timeframe": timeframe,
        "execution": execution,
        "params": params,
        "trades": len(result.trades),
        "win_rate": result.win_rate,
        "profit_factor": result.profit_factor,
        "average_r": result.average_r,
        "cagr": result.cagr,
        "max_drawdown": result.max_drawdown,
        "return_over_drawdown": result.return_over_drawdown,
        "calmar": result.calmar,
        "time_in_market": result.time_in_market,
        "costs": result.costs,
        "ending": result.ending_equity,
        "net_return": result.net_return,
        "fill_rate": result.fill_rate,
        "signals": result.signals,
        "fills": result.fills,
        "hold_ending": hold["ending"],
        "hold_cagr": hold["cagr"],
        "hold_drawdown": hold["max_drawdown"],
        "hold_ratio": hold["ratio"],
    }


def _choose(rows: list[dict[str, object]]) -> tuple[dict[str, object], bool]:
    eligible = [row for row in rows if int(row["trades"]) >= MIN_TRADES]
    pool = eligible or rows
    def key(row: dict[str, object]) -> tuple[Decimal, Decimal]:
        calmar = row["calmar"]
        net = row["net_return"]
        calmar_value = calmar if isinstance(calmar, Decimal) else Decimal("-999")
        net_value = net if isinstance(net, Decimal) else Decimal("-999")
        return calmar_value, net_value

    best = max(pool, key=key)
    return best, bool(eligible)


def _walk_forward(
    bars: list[Bar],
    variant: str,
    timeframe: TimeframeName,
    pair: str,
    grid: list[dict[str, str]],
    cut: int,
    atr_at: list[Decimal | None],
) -> dict[str, object]:
    years = _year_windows(bars, cut)
    tested: list[dict[str, object]] = []
    for train_end, test_end, label in years:
        scored = [
            _row(variant, pair, timeframe, params, "market", _run(bars, variant, timeframe, params, 0, train_end, "market", atr_at), _benchmark(bars, 0, train_end), "wf-train")
            for params in grid
        ]
        best, trusted = _choose(scored)
        params = dict(best["params"])  # type: ignore[arg-type]
        result = _run(bars, variant, timeframe, params, train_end, test_end, "market", atr_at)
        hold = _benchmark(bars, train_end, test_end)
        tested.append(
            {
                "year": label,
                "trusted_train": trusted,
                "trades": len(result.trades),
                "calmar": result.calmar,
                "hold_calmar": _calmar(hold["cagr"], hold["max_drawdown"]),
                "net_return": result.net_return,
                "hold_return": hold["net_return"],
            }
        )
    usable = [item for item in tested if int(item["trades"]) >= MIN_TRADES]
    beats = 0
    for item in usable:
        calmar = item["calmar"]
        hold_calmar = item["hold_calmar"]
        if isinstance(calmar, Decimal) and isinstance(hold_calmar, Decimal) and calmar > hold_calmar:
            beats += 1
    calmars = [item["calmar"] for item in usable if isinstance(item["calmar"], Decimal)]
    median = None
    if calmars:
        ordered = sorted(calmars)
        median = ordered[len(ordered) // 2]
    return {
        "variant": variant,
        "pair": pair,
        "timeframe": timeframe,
        "years": tested,
        "usable_years": len(usable),
        "beats_hold": beats,
        "median_calmar": median,
    }


def _year_windows(bars: list[Bar], cut: int) -> list[tuple[int, int, str]]:
    windows: list[tuple[int, int, str]] = []
    first_year = bars[0].open_time.year + 3
    last_year = bars[cut - 1].open_time.year if cut > 0 else bars[0].open_time.year
    for year in range(first_year, last_year + 1):
        train_end = _index_at(bars, datetime(year, 1, 1, tzinfo=timezone.utc))
        test_end = cut if year == last_year else _index_at(bars, datetime(year + 1, 1, 1, tzinfo=timezone.utc))
        test_end = min(test_end, cut)
        if train_end < 100 or test_end - train_end < 30:
            continue
        windows.append((train_end, test_end, str(year)))
    return windows


def _index_at(bars: list[Bar], moment: datetime) -> int:
    for index, bar in enumerate(bars):
        if bar.open_time >= moment:
            return index
    return len(bars)


def _calmar(cagr: Decimal | None, drawdown: Decimal | None) -> Decimal | None:
    if cagr is None or drawdown is None or drawdown <= 0:
        return None
    return cagr / drawdown


def _xrocket(
    frozen: dict[tuple[str, str, str], dict[str, str]],
    maker_frozen: dict[tuple[str, str, str], dict[str, str]],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    mapping = {"BTCUSDT": "BTC-USDT", "ETHUSDT": "ETH-USDT"}
    for timeframe in ("4h", "1h"):
        for pair, symbol in mapping.items():
            try:
                bars = asyncio.run(
                    load_history(
                        symbol,
                        XROCKET_DAYS,
                        rest_url=history_rest_url("mainnet"),
                        cache_dir=history_cache_dir(Path("data/backtests"), "mainnet"),
                        timeframe=timeframe,
                    )
                )
            except Exception as exc:
                rows.append({"pair": symbol, "timeframe": timeframe, "error": str(exc)})
                continue
            if len(bars) < 50:
                rows.append({"pair": symbol, "timeframe": timeframe, "error": "too few candles"})
                continue
            atr_at = _align_atr(bars)
            hold = _benchmark(bars, 0, len(bars))
            for variant in ("regime", "donchian", "donchian_regime", "grid"):
                params = frozen.get((variant, pair, timeframe))
                if params is None:
                    continue
                result = _run(bars, variant, timeframe, params, 0, len(bars), "market", atr_at)
                row = _row(variant, symbol, timeframe, params, "market", result, hold, "xrocket")
                row["period_start"] = bars[0].open_time.isoformat()
                row["period_end"] = bars[-1].open_time.isoformat()
                row["bars"] = len(bars)
                rows.append(row)
            for variant in ("regime", "donchian", "donchian_regime"):
                params = maker_frozen.get((variant, pair, timeframe))
                if params is None:
                    continue
                result = _run(bars, variant, timeframe, params, 0, len(bars), "maker", atr_at)
                row = _row(f"{variant}_maker", symbol, timeframe, params, "maker", result, hold, "xrocket")
                row["period_start"] = bars[0].open_time.isoformat()
                row["period_end"] = bars[-1].open_time.isoformat()
                row["bars"] = len(bars)
                rows.append(row)
            print(f"xrocket {symbol} {timeframe} {len(bars)}", flush=True)
    return rows


def _jsonable(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _markdown(payload: dict[str, object]) -> str:
    counts = payload["combinations"]
    lines = [
        "# Исследование входов",
        "",
        "Живая стратегия и её дефолты не менялись. Подбор шёл только на истории до последних 365 дней.",
        "Последние 365 дней прогнаны один раз, без повторного подбора.",
        "Вариант 1 ставит почти весь капитал. Пробой рискует 1% на стоп с учётом комиссии и проскальзывания. Сетка делит капитал по уровням.",
        f"Минимум сделок, чтобы комбинация участвовала в выборе: {payload['min_trades']}. Меньше этого вывод не делается.",
        "Walk-forward повторяет ту же сетку по годам внутри in-sample и не смотрит последние 365 дней.",
        "",
        "## Сколько комбинаций смотрели",
        "",
    ]
    if isinstance(counts, dict):
        for name, count in counts.items():
            lines.append(f"- {name}: {count}")
    lines.extend(["", "## In-sample, выбранные параметры", ""])
    lines.append(_table(payload["chosen"] if isinstance(payload["chosen"], list) else []))
    lines.extend(["", "## Out-of-sample, один прогон", ""])
    lines.append(_table(payload["out_of_sample"] if isinstance(payload["out_of_sample"], list) else []))
    lines.extend(["", "## Walk-forward", ""])
    lines.append(_wf_table(payload["walk_forward"] if isinstance(payload["walk_forward"], list) else []))
    lines.extend(["", "## xRocket mainnet, те же параметры", ""])
    lines.append(_table(payload["xrocket"] if isinstance(payload["xrocket"], list) else []))
    lines.append("")
    return "\n".join(lines)


def _table(rows: list[object]) -> str:
    header = "| Вариант | Пара | ТФ | Сделки | Win | PF | Ср. R | CAGR | Просадка | Доход/просадка | В рынке | Издержки | Fill | Купил-держал CAGR | Просадка удержания | Параметры |"
    sep = "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"
    body = [header, sep]
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        if "error" in raw:
            body.append(f"| {raw.get('pair', '')} | {raw.get('timeframe', '')} | | | | | | | | | | | | {raw['error']} |")
            continue
        body.append(
            "| "
            + " | ".join(
                [
                    str(raw.get("variant", "")),
                    str(raw.get("pair", "")),
                    str(raw.get("timeframe", "")),
                    str(raw.get("trades", "")),
                    _pct(raw.get("win_rate")),
                    _num(raw.get("profit_factor")),
                    _num(raw.get("average_r")),
                    _pct(raw.get("cagr")),
                    _pct(raw.get("max_drawdown")),
                    _num(raw.get("return_over_drawdown")),
                    _pct(raw.get("time_in_market")),
                    _num(raw.get("costs")),
                    _pct(raw.get("fill_rate")),
                    _pct(raw.get("hold_cagr")),
                    _pct(raw.get("hold_drawdown")),
                    str(raw.get("params", "")),
                ]
            )
            + " |"
        )
    return "\n".join(body)


def _wf_table(rows: list[object]) -> str:
    header = "| Вариант | Пара | ТФ | Годы со сделками ≥ 10 | Из них лучше удержания по Calmar | Медиана Calmar |"
    body = [header, "| --- | --- | --- | --- | --- | --- |"]
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        body.append(
            f"| {raw.get('variant')} | {raw.get('pair')} | {raw.get('timeframe')} | {raw.get('usable_years')} | {raw.get('beats_hold')} | {_num(raw.get('median_calmar'))} |"
        )
    return "\n".join(body)


def _pct(value: object) -> str:
    if not isinstance(value, str):
        return "—"
    try:
        number = Decimal(value) * Decimal("100")
    except Exception:
        return "—"
    return f"{number:.2f}%"


def _num(value: object) -> str:
    if not isinstance(value, str) or value == "":
        return "—"
    try:
        return f"{Decimal(value):.2f}"
    except Exception:
        return "—"
