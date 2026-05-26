#!/usr/bin/env python3
"""Research TradingView-style XAUUSD 4H trend strategy clones.

This is intentionally standalone: it does not register or run any live strategy.
It fetches hourly gold data, resamples to 4H, sweeps three hypothesis families,
and ranks them against the screenshot metrics.
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT / "engine" / "data" / "cache" / "gold_clone"
OUT_DIR = ROOT / "engine" / "data" / "research" / "gold_trend_clone"

TARGET = {
    "total_return": 0.5753,
    "max_drawdown": 0.0551,
    "trades": 213,
    "win_rate": 0.4507,
    "profit_factor": 1.802,
}


@dataclass(frozen=True)
class Params:
    family: str
    fast: int = 9
    slow: int = 21
    trend: int = 101
    atr: int = 14
    stop_atr: float = 2.0
    target_atr: float = 0.0
    trail_atr: float = 0.0
    max_hold_bars: int = 0
    channel_len: int = 20
    channel_atr: float = 1.5
    donchian_len: int = 20
    max_extension_atr: float = 999.0
    risk_fraction: float = 1.0

    def name(self) -> str:
        bits = [self.family, f"f{self.fast}", f"s{self.slow}", f"t{self.trend}", f"atr{self.atr}"]
        if self.family == "ema_cross_trend_atr":
            bits += [f"st{self.stop_atr}", f"tg{self.target_atr}", f"tr{self.trail_atr}", f"mh{self.max_hold_bars}", f"ext{self.max_extension_atr}"]
        elif self.family == "keltner_cross_trend":
            bits += [f"ch{self.channel_len}", f"ka{self.channel_atr}", f"st{self.stop_atr}", f"tg{self.target_atr}", f"tr{self.trail_atr}"]
        elif self.family == "donchian_trend_atr":
            bits += [f"don{self.donchian_len}", f"st{self.stop_atr}", f"tg{self.target_atr}", f"tr{self.trail_atr}", f"mh{self.max_hold_bars}"]
        elif self.family == "supertrend_ma_filter":
            bits += [f"fac{self.channel_atr}", f"len{self.channel_len}", f"st{self.stop_atr}", f"tg{self.target_atr}", f"tr{self.trail_atr}"]
        return "_".join(str(x).replace(".", "p") for x in bits)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sweep XAUUSD 4H trend-following clone candidates")
    p.add_argument("--start", default="2024-06-01")
    p.add_argument("--end", default="2026-05-05")
    p.add_argument("--ticker", default="GC=F", help="Yahoo ticker. GC=F is liquid proxy; XAUUSD=X may be sparse.")
    p.add_argument("--symbol", default="XAU/USDT", help="OKX swap symbol used when --source=okx")
    p.add_argument("--symbols", default="", help="Comma-separated OKX symbols for fixed supertrend validation")
    p.add_argument("--source", choices=["auto", "okx", "yfinance"], default="auto")
    p.add_argument("--interval", default="1h")
    p.add_argument("--top", type=int, default=20)
    p.add_argument("--max-combos", type=int, default=0, help="0 means full sweep")
    p.add_argument("--family", choices=["all", "ema", "keltner", "donchian", "supertrend"], default="all")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--fixed-supertrend", action="store_true", help="Evaluate fixed Supertrend(10,1.5)+EMA101+EMA9/21 variants across --symbols")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.fixed_supertrend:
        return run_fixed_supertrend_validation(args)
    raw = load_data(args)
    bars = to_4h(raw, start=args.start, end=args.end)
    bars = add_indicators(bars)
    combos = list(param_grid(args.family))
    if args.max_combos and args.max_combos > 0:
        combos = combos[: int(args.max_combos)]

    rows: list[dict[str, Any]] = []
    best_trades: pd.DataFrame | None = None
    best_equity: pd.DataFrame | None = None
    best_score = -1e18
    for params in combos:
        trades, equity, metrics = run_backtest(bars, params)
        sized = fit_position_size(metrics)
        metrics = {**metrics, **sized}
        score = clone_score(metrics, bars)
        row = {
            "score": score,
            "name": params.name(),
            "family": params.family,
            **params.__dict__,
            **metrics,
        }
        rows.append(row)
        if score > best_score:
            best_score = score
            best_trades = trades
            best_equity = equity.assign(equity_sized=1.0 + (equity["equity"] - 1.0) * float(metrics["fit_fraction"]))

    out_dir = OUT_DIR / datetime.now(timezone.utc).strftime("xau_4h_clone_%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    results = pd.DataFrame(rows).sort_values("score", ascending=False)
    results.to_csv(out_dir / "sweep_results.csv", index=False)
    results.head(args.top).to_csv(out_dir / "top.csv", index=False)
    if best_trades is not None:
        best_trades.to_csv(out_dir / "best_trades.csv", index=False)
    if best_equity is not None:
        best_equity.to_csv(out_dir / "best_equity.csv", index=False)
    write_report(out_dir, args, bars, results)
    print(json.dumps({"ok": True, "out_dir": str(out_dir), "top": results.head(5).to_dict("records")}, indent=2, default=str))
    return 0


def run_fixed_supertrend_validation(args: argparse.Namespace) -> int:
    symbols = [item.strip() for item in str(args.symbols or args.symbol).split(",") if item.strip()]
    variants = [
        Params("supertrend_ma_filter", fast=9, slow=21, trend=101, atr=10, channel_len=10, channel_atr=1.5, stop_atr=1.5, target_atr=3.0),
        Params("supertrend_ma_filter", fast=9, slow=21, trend=101, atr=10, channel_len=10, channel_atr=1.5, stop_atr=2.0, target_atr=3.0),
        Params("supertrend_ma_filter", fast=9, slow=21, trend=101, atr=10, channel_len=10, channel_atr=1.5, stop_atr=2.5, target_atr=3.0),
        Params("supertrend_ma_filter", fast=9, slow=21, trend=101, atr=10, channel_len=10, channel_atr=1.5, stop_atr=2.0, target_atr=4.0),
    ]
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for symbol in symbols:
        try:
            local_args = argparse.Namespace(**vars(args))
            local_args.symbol = symbol
            raw = load_data(local_args)
            bars = add_indicators(to_4h(raw, start=args.start, end=args.end))
            for params in variants:
                trades, equity, metrics = run_backtest(bars, params)
                rows.append(
                    {
                        "symbol": symbol,
                        "bars": len(bars),
                        "first_bar": str(bars.index.min()),
                        "last_bar": str(bars.index.max()),
                        "variant": params.name(),
                        **params.__dict__,
                        **metrics,
                    }
                )
        except Exception as exc:
            errors.append({"symbol": symbol, "error": str(exc)})
    out_dir = OUT_DIR / datetime.now(timezone.utc).strftime("fixed_supertrend_%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows)
    if not df.empty:
        df["fitness"] = df.apply(_fixed_validation_score, axis=1)
        df = df.sort_values(["symbol", "fitness"], ascending=[True, False])
        df.to_csv(out_dir / "fixed_supertrend_results.csv", index=False)
        best = df.sort_values("fitness", ascending=False).groupby("symbol", as_index=False).head(1).copy()
        best.to_csv(out_dir / "best_by_symbol.csv", index=False)
    else:
        best = pd.DataFrame()
    report = fixed_validation_report(args, df, best, errors)
    (out_dir / "report.md").write_text(report, encoding="utf-8")
    (out_dir / "summary.json").write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "symbols": symbols,
                "rows": rows,
                "errors": errors,
                "out_dir": str(out_dir),
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    print(json.dumps({"ok": True, "out_dir": str(out_dir), "errors": errors, "best": best.to_dict("records")}, indent=2, default=str))
    return 0 if rows else 1


def _fixed_validation_score(row: pd.Series) -> float:
    pf = float(row.get("profit_factor") or 0.0)
    wr = float(row.get("win_rate") or 0.0)
    ret = float(row.get("total_return_raw") or 0.0)
    dd = float(row.get("max_drawdown_raw") or 0.0)
    trades = float(row.get("trades") or 0.0)
    return pf * 1.5 + wr + ret * 1.0 - dd * 1.0 + min(trades, 80.0) / 160.0


def fixed_validation_report(args: argparse.Namespace, df: pd.DataFrame, best: pd.DataFrame, errors: list[dict[str, str]]) -> str:
    lines = [
        "# Fixed Supertrend Missile Validation",
        "",
        f"Generated: `{datetime.now(timezone.utc).isoformat()}`",
        f"Period request: `{args.start}` to `{args.end}`; data source `{args.source}`",
        "",
        "Tested rule:",
        "",
        "```text",
        "Supertrend factor=1.5 length=10",
        "EMA101 trend filter",
        "EMA9/EMA21 trigger",
        "Variants: stop ATR 1.5/2.0/2.5, target ATR 3.0/4.0",
        "```",
        "",
    ]
    if errors:
        lines += ["Data errors:", "", "| symbol | error |", "|---|---|"]
        for item in errors:
            lines.append(f"| `{item.get('symbol')}` | {str(item.get('error')).replace('|', '/')} |")
        lines.append("")
    if best.empty:
        lines.append("No successful backtests.")
        return "\n".join(lines)
    lines += [
        "Best variant by symbol:",
        "",
        "| symbol | bars | trades | win_rate | PF | return | max_DD | stop | target | first_bar |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in best.sort_values("fitness", ascending=False).to_dict("records"):
        lines.append(
            "| `{symbol}` | {bars} | {trades} | {wr:.2%} | {pf:.3f} | {ret:.2%} | {dd:.2%} | {stop:.1f} | {target:.1f} | {first} |".format(
                symbol=row.get("symbol"),
                bars=int(row.get("bars") or 0),
                trades=int(row.get("trades") or 0),
                wr=float(row.get("win_rate") or 0.0),
                pf=float(row.get("profit_factor") or 0.0),
                ret=float(row.get("total_return_raw") or 0.0),
                dd=float(row.get("max_drawdown_raw") or 0.0),
                stop=float(row.get("stop_atr") or 0.0),
                target=float(row.get("target_atr") or 0.0),
                first=str(row.get("first_bar") or "")[:10],
            )
        )
    lines += [
        "",
        "Interpretation guardrail:",
        "",
        "- PF/win rate are more important than return because sizing can scale return and drawdown.",
        "- Short data windows, especially newly listed OKX stock tokens or XAU swap, can overstate or understate edge.",
        "- A robust clone should keep PF > 1.2 across multiple liquid symbols, not only XAU.",
    ]
    return "\n".join(lines)


def load_data(args: argparse.Namespace) -> pd.DataFrame:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_key = f"{args.source}_{args.symbol.replace('/', '_')}_{args.ticker.replace('=', '_').replace('^', '')}_{args.interval}_{args.start}_{args.end}"
    cache_path = CACHE_DIR / f"{cache_key}.csv"
    if cache_path.exists() and not args.refresh:
        df = pd.read_csv(cache_path, parse_dates=["Datetime"], index_col="Datetime")
        return normalize_ohlcv(df)
    errors: list[str] = []
    if args.source in {"auto", "okx"}:
        try:
            raw = fetch_okx_swap(args.symbol, args.start, args.end, args.interval)
            raw.reset_index().rename(columns={"index": "Datetime"}).to_csv(cache_path, index=False)
            return normalize_ohlcv(raw)
        except Exception as exc:
            errors.append(f"okx: {exc}")
            if args.source == "okx":
                raise
    if args.source in {"auto", "yfinance"}:
        try:
            raw = fetch_yfinance(args)
            raw.reset_index().rename(columns={"index": "Datetime"}).to_csv(cache_path, index=False)
            return normalize_ohlcv(raw)
        except Exception as exc:
            errors.append(f"yfinance: {exc}")
            raise RuntimeError("; ".join(errors)) from exc
    raise RuntimeError("; ".join(errors) or "no data source attempted")


def fetch_yfinance(args: argparse.Namespace) -> pd.DataFrame:
    try:
        import yfinance as yf
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("yfinance is required") from exc
    raw = yf.download(
        args.ticker,
        start=args.start,
        end=args.end,
        interval=args.interval,
        progress=False,
        auto_adjust=False,
    )
    if raw.empty and args.ticker != "XAUUSD=X":
        raw = yf.download("XAUUSD=X", start=args.start, end=args.end, interval=args.interval, progress=False, auto_adjust=False)
    if raw.empty:
        raise RuntimeError(f"empty yfinance frame for {args.ticker}")
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = [str(col[0]) for col in raw.columns]
    raw = raw.reset_index()
    time_col = "Datetime" if "Datetime" in raw.columns else "Date"
    raw = raw.rename(columns={time_col: "Datetime"})
    return raw.set_index("Datetime")


def fetch_okx_swap(symbol: str, start: str, end: str, interval: str) -> pd.DataFrame:
    import time

    import requests

    bar = interval.upper()
    inst_id = _okx_inst_id(symbol)
    since = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
    end_ms = int(pd.Timestamp(end, tz="UTC").timestamp() * 1000)
    cursor = end_ms
    rows: list[list[str]] = []
    last_error: Exception | None = None
    session = requests.Session()
    while cursor > since:
        try:
            resp = session.get(
                "https://www.okx.com/api/v5/market/history-candles",
                params={"instId": inst_id, "bar": bar, "limit": "300", "after": str(cursor)},
                timeout=20,
            )
            resp.raise_for_status()
            payload = resp.json()
            if str(payload.get("code")) != "0":
                raise RuntimeError(payload.get("msg") or payload)
            batch = payload.get("data") or []
            if not batch:
                break
            rows.extend(batch)
            oldest = min(int(row[0]) for row in batch)
            if oldest >= cursor:
                break
            cursor = oldest
            time.sleep(0.12)
        except Exception as exc:
            last_error = exc
            break
    if not rows:
        raise RuntimeError(f"empty OKX OHLCV for {inst_id}: {last_error}")
    df = pd.DataFrame(rows, columns=["Datetime", "open", "high", "low", "close", "volume", "vol_ccy", "vol_ccy_quote", "confirm"])
    df["Datetime"] = pd.to_datetime(pd.to_numeric(df["Datetime"]), unit="ms", utc=True)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.drop_duplicates("Datetime").set_index("Datetime").sort_index()
    df = df.loc[pd.Timestamp(start, tz="UTC") : pd.Timestamp(end, tz="UTC")]
    return df[["open", "high", "low", "close", "volume"]].dropna()


def _okx_inst_id(symbol: str) -> str:
    value = symbol.strip().upper()
    if value.endswith("-SWAP"):
        return value
    base = value.split(":", 1)[0].replace("/", "-")
    if base.endswith("-USDT"):
        return f"{base}-SWAP"
    return f"{base}-USDT-SWAP"


def normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    rename = {c: str(c).lower() for c in df.columns}
    out = df.rename(columns=rename)
    need = ["open", "high", "low", "close"]
    for col in need:
        if col not in out.columns:
            raise RuntimeError(f"missing {col} in data")
    if "volume" not in out.columns:
        out["volume"] = 0.0
    out.index = pd.to_datetime(out.index, utc=True)
    return out[["open", "high", "low", "close", "volume"]].sort_index().dropna()


def to_4h(raw: pd.DataFrame, *, start: str, end: str) -> pd.DataFrame:
    bars = raw.resample("4h", label="right", closed="right").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    )
    bars = bars.dropna()
    return bars.loc[pd.Timestamp(start, tz="UTC") : pd.Timestamp(end, tz="UTC")].copy()


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for span in sorted({8, 9, 10, 13, 20, 21, 26, 34, 55, 89, 101, 144, 200}):
        out[f"ema_{span}"] = out["close"].ewm(span=span, adjust=False).mean()
    prev = out["close"].shift(1)
    tr = pd.concat([(out["high"] - out["low"]).abs(), (out["high"] - prev).abs(), (out["low"] - prev).abs()], axis=1).max(axis=1)
    for n in [10, 14, 20, 30]:
        out[f"atr_{n}"] = tr.rolling(n).mean()
    for n in [10, 15, 20, 30, 40, 55]:
        out[f"hh_{n}"] = out["high"].rolling(n).max()
        out[f"ll_{n}"] = out["low"].rolling(n).min()
    return out.dropna()


def param_grid(family: str = "all") -> list[Params]:
    out: list[Params] = []
    if family in {"all", "ema"}:
        for fast, slow, trend, stop, target, trail, hold, ext in itertools.product(
        [8, 9, 10, 13],
        [20, 21, 26, 34],
        [55, 89, 101, 144, 200],
        [1.2, 1.5, 2.0, 2.5, 3.0],
        [0.0, 2.0, 3.0, 4.0, 5.0],
        [0.0, 1.5, 2.0, 2.5, 3.0],
        [0, 12, 18, 30, 45],
        [1.5, 2.0, 3.0, 999.0],
        ):
            if fast < slow:
                out.append(Params("ema_cross_trend_atr", fast=fast, slow=slow, trend=trend, stop_atr=stop, target_atr=target, trail_atr=trail, max_hold_bars=hold, max_extension_atr=ext))
    if family in {"all", "keltner"}:
        for fast, slow, trend, ch, ka, stop, target, trail in itertools.product(
        [9, 13],
        [21, 34],
        [89, 101, 144, 200],
        [10, 20, 30],
        [1.0, 1.5, 2.0, 2.5],
        [1.5, 2.0, 2.5, 3.0],
        [0.0, 2.0, 3.0, 4.0],
        [0.0, 2.0, 2.5, 3.0],
        ):
            out.append(Params("keltner_cross_trend", fast=fast, slow=slow, trend=trend, channel_len=ch, channel_atr=ka, stop_atr=stop, target_atr=target, trail_atr=trail))
    if family in {"all", "donchian"}:
        for trend, don, stop, target, trail, hold in itertools.product(
        [89, 101, 144, 200],
        [10, 15, 20, 30, 40, 55],
        [1.5, 2.0, 2.5, 3.0],
        [0.0, 2.0, 3.0, 4.0],
        [0.0, 2.0, 2.5, 3.0],
        [0, 18, 30, 45],
        ):
            out.append(Params("donchian_trend_atr", trend=trend, donchian_len=don, stop_atr=stop, target_atr=target, trail_atr=trail, max_hold_bars=hold))
    if family in {"all", "supertrend"}:
        for trend, factor, length, fast, slow, stop, target, trail in itertools.product(
            [101, 89, 144],
            [1.5, 1.0, 2.0, 2.5],
            [10, 14, 20],
            [9, 8, 10],
            [21, 20, 26],
            [1.0, 1.5, 2.0, 2.5],
            [0.0, 2.0, 3.0, 4.0],
            [0.0, 1.5, 2.0, 2.5],
        ):
            if fast < slow:
                out.append(
                    Params(
                        "supertrend_ma_filter",
                        fast=fast,
                        slow=slow,
                        trend=trend,
                        atr=length,
                        stop_atr=stop,
                        target_atr=target,
                        trail_atr=trail,
                        channel_len=length,
                        channel_atr=factor,
                    )
                )
    return out


def run_backtest(bars: pd.DataFrame, p: Params) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    if p.family == "supertrend_ma_filter":
        st_col = supertrend_col(p.channel_len, p.channel_atr)
        if st_col not in bars.columns:
            bars = bars.copy()
            bars[st_col] = supertrend_series(bars, p.channel_len, p.channel_atr)
    pos = 0
    entry = 0.0
    entry_i = -1
    entry_ts = None
    stop = math.nan
    trail = math.nan
    target = math.nan
    trade_rows: list[dict[str, Any]] = []
    eq = 1.0
    equity_rows: list[dict[str, Any]] = []
    pending_signal = 0
    pending_reason = ""
    fee = 0.00025
    slip = 0.00015

    for i, (ts, row) in enumerate(bars.iterrows()):
        if i < 2:
            equity_rows.append({"ts": ts, "equity": eq})
            continue

        if pending_signal and pos == 0:
            px = float(row["open"]) * (1.0 + slip * pending_signal)
            pos = pending_signal
            entry = px
            entry_i = i
            entry_ts = ts
            atr = float(row[f"atr_{p.atr}"])
            stop = entry - pos * p.stop_atr * atr
            target = entry + pos * p.target_atr * atr if p.target_atr > 0 else math.nan
            trail = stop
            pending_signal = 0
            pending_reason = ""

        if pos != 0:
            atr = float(row[f"atr_{p.atr}"])
            if p.trail_atr > 0:
                trail_candidate = float(row["close"]) - pos * p.trail_atr * atr
                trail = max(trail, trail_candidate) if pos > 0 else min(trail, trail_candidate)
                stop = trail
            exit_reason = ""
            exit_px = math.nan
            stop_hit = (pos > 0 and float(row["low"]) <= stop) or (pos < 0 and float(row["high"]) >= stop)
            target_hit = p.target_atr > 0 and ((pos > 0 and float(row["high"]) >= target) or (pos < 0 and float(row["low"]) <= target))
            reverse = signal_at(bars, i, p) == -pos
            time_stop = bool(p.max_hold_bars and i - entry_i >= p.max_hold_bars)
            if stop_hit and target_hit:
                exit_reason = "same_bar_stop_first"
                exit_px = stop
            elif target_hit:
                exit_reason = "target"
                exit_px = target
            elif stop_hit:
                exit_reason = "atr_stop"
                exit_px = stop
            elif reverse:
                exit_reason = "reverse_signal"
                exit_px = float(row["close"])
            elif time_stop:
                exit_reason = "time_stop"
                exit_px = float(row["close"])
            if exit_reason:
                exit_px = exit_px * (1.0 - slip * pos)
                gross = pos * (exit_px / entry - 1.0)
                net = gross - 2.0 * fee
                trade_rows.append(
                    {
                        "entry_ts": entry_ts,
                        "exit_ts": ts,
                        "side": "long" if pos > 0 else "short",
                        "entry": entry,
                        "exit": exit_px,
                        "reason": exit_reason,
                        "gross_return": gross,
                        "net_return": net,
                        "bars": i - entry_i,
                    }
                )
                eq *= 1.0 + net
                pos = 0
                entry = 0.0
                entry_i = -1
                entry_ts = None
                stop = math.nan
                trail = math.nan
                target = math.nan
                if reverse:
                    pending_signal = signal_at(bars, i, p)
                    pending_reason = "reverse"

        if pos == 0 and not pending_signal:
            sig = signal_at(bars, i, p)
            if sig:
                pending_signal = sig
                pending_reason = "entry"

        equity_rows.append({"ts": ts, "equity": eq, "position": pos, "pending_reason": pending_reason})

    trades = pd.DataFrame(trade_rows)
    equity = pd.DataFrame(equity_rows)
    metrics = compute_metrics(trades, equity)
    return trades, equity, metrics


def signal_at(bars: pd.DataFrame, i: int, p: Params) -> int:
    row = bars.iloc[i]
    prev = bars.iloc[i - 1]
    close = float(row["close"])
    trend = float(row[f"ema_{p.trend}"])
    atr = float(row[f"atr_{p.atr}"])
    if not np.isfinite(atr) or atr <= 0:
        return 0
    if p.family == "ema_cross_trend_atr":
        f = float(row[f"ema_{p.fast}"])
        s = float(row[f"ema_{p.slow}"])
        pf = float(prev[f"ema_{p.fast}"])
        ps = float(prev[f"ema_{p.slow}"])
        cross_up = pf <= ps and f > s
        cross_dn = pf >= ps and f < s
        extension = abs(close - trend) / atr
        if cross_up and close > trend and extension <= p.max_extension_atr:
            return 1
        if cross_dn and close < trend and extension <= p.max_extension_atr:
            return -1
        return 0
    if p.family == "keltner_cross_trend":
        mid = float(row[f"ema_{p.channel_len}"]) if f"ema_{p.channel_len}" in row else float(row[f"ema_{p.slow}"])
        upper = mid + p.channel_atr * atr
        lower = mid - p.channel_atr * atr
        prev_mid = float(prev[f"ema_{p.channel_len}"]) if f"ema_{p.channel_len}" in prev else float(prev[f"ema_{p.slow}"])
        prev_upper = prev_mid + p.channel_atr * float(prev[f"atr_{p.atr}"])
        prev_lower = prev_mid - p.channel_atr * float(prev[f"atr_{p.atr}"])
        if float(prev["close"]) <= prev_upper and close > upper and close > trend:
            return 1
        if float(prev["close"]) >= prev_lower and close < lower and close < trend:
            return -1
        return 0
    if p.family == "donchian_trend_atr":
        hh = float(prev[f"hh_{p.donchian_len}"])
        ll = float(prev[f"ll_{p.donchian_len}"])
        if close > hh and close > trend:
            return 1
        if close < ll and close < trend:
            return -1
        return 0
    if p.family == "supertrend_ma_filter":
        st_col = supertrend_col(p.channel_len, p.channel_atr)
        st_now = int(row[st_col])
        st_prev = int(prev[st_col])
        f = float(row[f"ema_{p.fast}"])
        s = float(row[f"ema_{p.slow}"])
        pf = float(prev[f"ema_{p.fast}"])
        ps = float(prev[f"ema_{p.slow}"])
        cross_up = pf <= ps and f > s
        cross_dn = pf >= ps and f < s
        flip_up = st_now > 0 and st_prev <= 0
        flip_dn = st_now < 0 and st_prev >= 0
        if st_now > 0 and close > trend and f > s and (cross_up or flip_up):
            return 1
        if st_now < 0 and close < trend and f < s and (cross_dn or flip_dn):
            return -1
        return 0
    return 0


def supertrend_col(length: int, factor: float) -> str:
    return f"supertrend_dir_{length}_{str(factor).replace('.', 'p')}"


def supertrend_series(bars: pd.DataFrame, length: int, factor: float) -> pd.Series:
    atr = bars[f"atr_{length}"]
    hl2 = (bars["high"] + bars["low"]) / 2.0
    upper = hl2 + factor * atr
    lower = hl2 - factor * atr
    final_upper = upper.copy()
    final_lower = lower.copy()
    direction = pd.Series(1, index=bars.index, dtype=int)
    for j in range(1, len(bars)):
        prev_close = float(bars["close"].iloc[j - 1])
        if upper.iloc[j] < final_upper.iloc[j - 1] or prev_close > final_upper.iloc[j - 1]:
            final_upper.iloc[j] = upper.iloc[j]
        else:
            final_upper.iloc[j] = final_upper.iloc[j - 1]
        if lower.iloc[j] > final_lower.iloc[j - 1] or prev_close < final_lower.iloc[j - 1]:
            final_lower.iloc[j] = lower.iloc[j]
        else:
            final_lower.iloc[j] = final_lower.iloc[j - 1]
        if direction.iloc[j - 1] < 0 and float(bars["close"].iloc[j]) > final_upper.iloc[j]:
            direction.iloc[j] = 1
        elif direction.iloc[j - 1] > 0 and float(bars["close"].iloc[j]) < final_lower.iloc[j]:
            direction.iloc[j] = -1
        else:
            direction.iloc[j] = direction.iloc[j - 1]
    return direction


def compute_metrics(trades: pd.DataFrame, equity: pd.DataFrame) -> dict[str, Any]:
    n = len(trades)
    if n == 0:
        return {"trades": 0, "win_rate": None, "profit_factor": None, "total_return_raw": 0.0, "max_drawdown_raw": 0.0}
    pnl = trades["net_return"].astype(float)
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    eq = equity["equity"].astype(float)
    dd = eq / eq.cummax() - 1.0
    return {
        "trades": int(n),
        "wins": int((pnl > 0).sum()),
        "losses": int((pnl < 0).sum()),
        "win_rate": float((pnl > 0).mean()),
        "profit_factor": float(wins.sum() / abs(losses.sum())) if len(losses) and abs(losses.sum()) > 0 else None,
        "avg_net_return": float(pnl.mean()),
        "median_net_return": float(pnl.median()),
        "total_return_raw": float(eq.iloc[-1] - 1.0),
        "max_drawdown_raw": float(abs(dd.min())),
    }


def fit_position_size(metrics: dict[str, Any]) -> dict[str, float]:
    raw_ret = float(metrics.get("total_return_raw") or 0.0)
    raw_dd = float(metrics.get("max_drawdown_raw") or 0.0)
    candidates = []
    for target_key, raw in [("return", raw_ret), ("dd", raw_dd)]:
        if raw > 0:
            target = TARGET["total_return"] if target_key == "return" else TARGET["max_drawdown"]
            candidates.append(target / raw)
    frac = float(np.median(candidates)) if candidates else 1.0
    frac = max(0.05, min(10.0, frac))
    return {
        "fit_fraction": frac,
        "total_return": raw_ret * frac,
        "max_drawdown": raw_dd * frac,
    }


def target_for_bars(bars: pd.DataFrame) -> dict[str, float]:
    # Screenshot covers roughly 2024-06-01..2026-05-04 on 4H bars.
    screenshot_4h_bars = 704 * 6
    scale = max(0.1, len(bars) / screenshot_4h_bars)
    target = dict(TARGET)
    target["trades"] = max(20.0, TARGET["trades"] * scale)
    return target


def clone_score(m: dict[str, Any], bars: pd.DataFrame) -> float:
    target = target_for_bars(bars)
    pf = float(m.get("profit_factor") or 0.0)
    wr = float(m.get("win_rate") or 0.0)
    trades = float(m.get("trades") or 0.0)
    ret = float(m.get("total_return") or 0.0)
    dd = float(m.get("max_drawdown") or 0.0)
    return -(
        abs(trades - target["trades"]) / target["trades"] * 2.5
        + abs(wr - target["win_rate"]) / target["win_rate"] * 1.5
        + abs(pf - target["profit_factor"]) / target["profit_factor"] * 2.0
        + abs(ret - target["total_return"]) / target["total_return"] * 0.75
        + abs(dd - target["max_drawdown"]) / target["max_drawdown"] * 0.75
    )


def write_report(out_dir: Path, args: argparse.Namespace, bars: pd.DataFrame, results: pd.DataFrame) -> None:
    top = results.head(10)
    best = top.iloc[0].to_dict() if len(top) else {}
    target = target_for_bars(bars)
    lines = [
        "# XAUUSD 4H Strategy Clone Research",
        "",
        f"Generated: `{datetime.now(timezone.utc).isoformat()}`",
        f"Ticker: `{args.ticker}` proxy, interval `{args.interval}` resampled to `4h`",
        f"Bars: `{len(bars)}` from `{bars.index.min()}` to `{bars.index.max()}`",
        "",
        "Target screenshot metrics:",
        "",
        "| metric | target |",
        "|---|---:|",
        f"| total_return | {TARGET['total_return']:.2%} |",
        f"| max_drawdown | {TARGET['max_drawdown']:.2%} |",
        f"| trades | {TARGET['trades']} screenshot / {target['trades']:.1f} sample-adjusted |",
        f"| win_rate | {TARGET['win_rate']:.2%} |",
        f"| profit_factor | {TARGET['profit_factor']:.3f} |",
        "",
        "Best candidates:",
        "",
        "| rank | family | trades | win_rate | PF | fit_return | fit_DD | raw_return | raw_DD | fit_fraction |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for idx, row in enumerate(top.to_dict("records"), start=1):
        lines.append(
            "| {idx} | `{family}` | {trades} | {wr:.2%} | {pf:.3f} | {ret:.2%} | {dd:.2%} | {raw_ret:.2%} | {raw_dd:.2%} | {frac:.2f} |".format(
                idx=idx,
                family=row.get("family"),
                trades=int(row.get("trades") or 0),
                wr=float(row.get("win_rate") or 0.0),
                pf=float(row.get("profit_factor") or 0.0),
                ret=float(row.get("total_return") or 0.0),
                dd=float(row.get("max_drawdown") or 0.0),
                raw_ret=float(row.get("total_return_raw") or 0.0),
                raw_dd=float(row.get("max_drawdown_raw") or 0.0),
                frac=float(row.get("fit_fraction") or 0.0),
            )
        )
    lines += [
        "",
        "Best parameter set:",
        "",
        "```json",
        json.dumps(best, indent=2, default=str),
        "```",
        "",
        "Interpretation:",
        "",
        "- `fit_fraction` is the notional/risk scalar required to compare with the screenshot equity scale.",
        "- Profit factor and win rate are independent of that scalar; these are the most important clone-shape checks.",
        "- This is a visual reverse-engineering candidate, not proof of the original TradingView source.",
    ]
    (out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
    (out_dir / "summary.json").write_text(
        json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(), "target": TARGET, "best": best}, indent=2, default=str),
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
