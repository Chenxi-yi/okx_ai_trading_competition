#!/usr/bin/env python3
"""Backtest modular OKX stock-token strategies with intraday trade management."""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT / "engine" / "data" / "cache"
EQUITY_DIR = CACHE_DIR / "us_equities_yfinance_1d"
OUT_ROOT = ROOT / "engine" / "data" / "research" / "okx_stock_token_modules"


@dataclass(frozen=True)
class Params:
    threshold: float = 0.01
    fee_bps_per_side: float = 5.0
    slippage_bps_per_side: float = 2.0
    stop_pct: float = 0.04
    target_pct: float = 0.06
    be_trigger_pct: float = 0.012
    lock1_trigger_pct: float = 0.02
    lock1_pct: float = 0.008
    lock2_trigger_pct: float = 0.03
    lock2_pct: float = 0.015
    max_hold_hours: float = 48.0
    cooldown_hours: float = 12.0
    overheat_pct: float = 0.06
    confirm_move_pct: float = 0.004
    confirm_max_adverse_pct: float = 0.015


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backtest OKX stock-token modules")
    p.add_argument("--symbols", default="AMD,AMZN,ARM,COIN,GOOGL,HOOD,INTC,MSTR,NVDA,PLTR,TSLA")
    p.add_argument("--start", default="2026-02-01")
    p.add_argument("--end", default="2026-05-15")
    p.add_argument("--threshold", type=float, default=0.01)
    p.add_argument("--stop-pct", type=float, default=0.04)
    p.add_argument("--target-pct", type=float, default=0.06)
    p.add_argument("--max-hold-hours", type=float, default=48.0)
    p.add_argument("--cooldown-hours", type=float, default=12.0)
    p.add_argument("--overheat-pct", type=float, default=0.06)
    p.add_argument("--confirm-move-pct", type=float, default=0.004)
    p.add_argument("--out-id", default=None)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    params = Params(
        threshold=float(args.threshold),
        stop_pct=float(args.stop_pct),
        target_pct=float(args.target_pct),
        max_hold_hours=float(args.max_hold_hours),
        cooldown_hours=float(args.cooldown_hours),
        overheat_pct=float(args.overheat_pct),
        confirm_move_pct=float(args.confirm_move_pct),
    )
    tickers = [item.strip().upper() for item in str(args.symbols).split(",") if item.strip()]
    all_trades: list[dict[str, Any]] = []
    for ticker in tickers:
        okx_daily = load_okx_daily(ticker, args.start, args.end)
        okx_5m = load_okx_5m(ticker, args.start, args.end)
        equity = load_equity_daily(ticker, args.start, args.end)
        if okx_daily.empty or okx_5m.empty or equity.empty:
            continue
        joined = okx_daily.join(equity, how="inner").dropna()
        if len(joined) < 8:
            continue
        joined["okx_ret1"] = joined["okx_close"].pct_change()
        joined["equity_ret1"] = joined["equity_close"].pct_change()
        joined["dislocation"] = joined["okx_ret1"] - joined["equity_ret1"]
        modules = build_signals(joined, params)
        for module, signals in modules.items():
            all_trades.extend(backtest_module(ticker, module, signals, okx_5m, params))

    summary = summarize(all_trades)
    out_dir = OUT_ROOT / (args.out_id or f"modules_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}")
    out_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(all_trades).to_csv(out_dir / "trades.csv", index=False)
    pd.DataFrame(summary).to_csv(out_dir / "summary.csv", index=False)
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "args": vars(args), "summary": summary}
    (out_dir / "summary.json").write_text(json.dumps(report, indent=2, sort_keys=True))
    (out_dir / "report.md").write_text(markdown_report(report))
    print(json.dumps({"out_dir": str(out_dir.relative_to(ROOT)), "summary": summary}, indent=2, sort_keys=True))
    return 0


def load_okx_daily(ticker: str, start: str, end: str) -> pd.DataFrame:
    path = CACHE_DIR / f"{ticker}_USDT_futures_1d.parquet"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_parquet(path).copy()
    df.index = pd.to_datetime(df.index, utc=True).tz_convert(None).normalize()
    df = df.sort_index()
    out = pd.DataFrame({"okx_close": pd.to_numeric(df["close"], errors="coerce")})
    return out.loc[(out.index >= pd.Timestamp(start)) & (out.index <= pd.Timestamp(end))].dropna()


def load_okx_5m(ticker: str, start: str, end: str) -> pd.DataFrame:
    path = CACHE_DIR / f"{ticker}_USDT_futures_5m.parquet"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_parquet(path).copy()
    df.index = pd.to_datetime(df.index, utc=True)
    df = df.sort_index()
    cols = ["open", "high", "low", "close"]
    for col in cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    start_ts = pd.Timestamp(start, tz="UTC")
    end_ts = pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)
    return df.loc[(df.index >= start_ts) & (df.index <= end_ts), cols].dropna()


def load_equity_daily(ticker: str, start: str, end: str) -> pd.DataFrame:
    path = EQUITY_DIR / f"{ticker}.csv"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path)
    if "date" not in df or "close" not in df:
        return pd.DataFrame()
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    df = df.set_index("date").sort_index()
    out = pd.DataFrame({"equity_close": pd.to_numeric(df["close"], errors="coerce")})
    return out.loc[(out.index >= pd.Timestamp(start)) & (out.index <= pd.Timestamp(end))].dropna()


def build_signals(df: pd.DataFrame, params: Params) -> dict[str, pd.DataFrame]:
    th = params.threshold
    rows: dict[str, pd.DataFrame] = {}

    legacy = pd.Series(0.0, index=df.index)
    legacy.loc[df["equity_ret1"] > th] = 1.0
    legacy.loc[df["equity_ret1"] < -th] = -1.0
    rows["legacy_equity_momentum"] = df.assign(signal=legacy)

    confirmed = pd.Series(0.0, index=df.index)
    same_up = (df["equity_ret1"] > th) & (df["okx_ret1"] > 0) & (df["okx_ret1"].abs() <= params.overheat_pct)
    same_down = (df["equity_ret1"] < -th) & (df["okx_ret1"] < 0) & (df["okx_ret1"].abs() <= params.overheat_pct)
    confirmed.loc[same_up] = 1.0
    confirmed.loc[same_down] = -1.0
    rows["equity_momentum_okx_confirmed"] = df.assign(signal=confirmed)

    okx_momo = pd.Series(0.0, index=df.index)
    okx_momo.loc[(df["okx_ret1"] > th) & (df["okx_ret1"].abs() <= params.overheat_pct)] = 1.0
    okx_momo.loc[(df["okx_ret1"] < -th) & (df["okx_ret1"].abs() <= params.overheat_pct)] = -1.0
    rows["okx_momentum_capped"] = df.assign(signal=okx_momo)

    disloc = pd.Series(0.0, index=df.index)
    disloc.loc[df["dislocation"] >= th] = -1.0
    disloc.loc[df["dislocation"] <= -th] = 1.0
    rows["dislocation_reversion_confirmed"] = df.assign(signal=disloc)
    return rows


def backtest_module(ticker: str, module: str, signals: pd.DataFrame, okx_5m: pd.DataFrame, params: Params) -> list[dict[str, Any]]:
    trades: list[dict[str, Any]] = []
    cooldown_until: pd.Timestamp | None = None
    sample = signals.dropna(subset=["signal"])
    for ts, row in sample.iterrows():
        side_raw = float(row["signal"])
        if side_raw == 0:
            continue
        entry_day = pd.Timestamp(ts).tz_localize("UTC") + pd.Timedelta(days=1)
        if cooldown_until is not None and entry_day < cooldown_until:
            continue
        side = "long" if side_raw > 0 else "short"
        window = okx_5m.loc[(okx_5m.index >= entry_day) & (okx_5m.index < entry_day + pd.Timedelta(hours=params.max_hold_hours))]
        if window.empty:
            continue
        if module == "dislocation_reversion_confirmed":
            trade = simulate_confirmed_reversion(ticker, module, side, ts, row, window, params)
        else:
            trade = simulate_trade(ticker, module, side, ts, row, window, params, entry_at_first_bar=True)
        if not trade:
            continue
        trades.append(trade)
        cooldown_until = pd.Timestamp(trade["exit_ts"]) + pd.Timedelta(hours=params.cooldown_hours)
    return trades


def simulate_confirmed_reversion(
    ticker: str,
    module: str,
    side: str,
    signal_ts: pd.Timestamp,
    signal_row: pd.Series,
    window: pd.DataFrame,
    params: Params,
) -> dict[str, Any] | None:
    day_open = float(window.iloc[0]["open"])
    adverse_limit = day_open * (1.0 - params.confirm_max_adverse_pct) if side == "long" else day_open * (1.0 + params.confirm_max_adverse_pct)
    confirm_px = day_open * (1.0 + params.confirm_move_pct) if side == "long" else day_open * (1.0 - params.confirm_move_pct)
    for idx, bar in window.iterrows():
        if side == "long":
            if float(bar["low"]) <= adverse_limit:
                return None
            if float(bar["high"]) >= confirm_px:
                sub = window.loc[window.index >= idx]
                return simulate_trade(ticker, module, side, signal_ts, signal_row, sub, params, entry_at_first_bar=False, entry_price=confirm_px)
        else:
            if float(bar["high"]) >= adverse_limit:
                return None
            if float(bar["low"]) <= confirm_px:
                sub = window.loc[window.index >= idx]
                return simulate_trade(ticker, module, side, signal_ts, signal_row, sub, params, entry_at_first_bar=False, entry_price=confirm_px)
    return None


def simulate_trade(
    ticker: str,
    module: str,
    side: str,
    signal_ts: pd.Timestamp,
    signal_row: pd.Series,
    window: pd.DataFrame,
    params: Params,
    *,
    entry_at_first_bar: bool,
    entry_price: float | None = None,
) -> dict[str, Any] | None:
    first = window.iloc[0]
    entry = float(first["open"] if entry_at_first_bar else entry_price)
    if entry <= 0 or not math.isfinite(entry):
        return None
    cost = 2.0 * (params.fee_bps_per_side + params.slippage_bps_per_side) / 10000.0
    stop = entry * (1.0 - params.stop_pct) if side == "long" else entry * (1.0 + params.stop_pct)
    target = entry * (1.0 + params.target_pct) if side == "long" else entry * (1.0 - params.target_pct)
    best_ret = 0.0
    worst_ret = 0.0
    exit_px = float(window.iloc[-1]["close"])
    exit_ts = window.index[-1]
    exit_reason = "max_hold"
    for idx, bar in window.iterrows():
        high = float(bar["high"])
        low = float(bar["low"])
        fav_px = high if side == "long" else low
        adv_px = low if side == "long" else high
        fav_ret = directional_return(side, entry, fav_px)
        adv_ret = directional_return(side, entry, adv_px)
        best_ret = max(best_ret, fav_ret)
        worst_ret = min(worst_ret, adv_ret)
        if best_ret >= params.lock2_trigger_pct:
            stop = max(stop, entry * (1.0 + params.lock2_pct)) if side == "long" else min(stop, entry * (1.0 - params.lock2_pct))
        elif best_ret >= params.lock1_trigger_pct:
            stop = max(stop, entry * (1.0 + params.lock1_pct)) if side == "long" else min(stop, entry * (1.0 - params.lock1_pct))
        elif best_ret >= params.be_trigger_pct:
            stop = max(stop, entry) if side == "long" else min(stop, entry)
        hit_stop = low <= stop if side == "long" else high >= stop
        hit_target = high >= target if side == "long" else low <= target
        if hit_stop and hit_target:
            exit_px = stop
            exit_ts = idx
            exit_reason = "stop_first_conservative"
            break
        if hit_stop:
            exit_px = stop
            exit_ts = idx
            exit_reason = "protected_stop" if best_ret >= params.be_trigger_pct else "hard_stop"
            break
        if hit_target:
            exit_px = target
            exit_ts = idx
            exit_reason = "target"
            break
    gross = directional_return(side, entry, exit_px)
    net = gross - cost
    return {
        "ticker": ticker,
        "symbol": f"{ticker}_USDT",
        "module": module,
        "side": side,
        "signal_ts": pd.Timestamp(signal_ts).isoformat(),
        "entry_ts": window.index[0].isoformat(),
        "exit_ts": pd.Timestamp(exit_ts).isoformat(),
        "entry": entry,
        "exit": exit_px,
        "gross_return": gross,
        "net_return": net,
        "mfe": best_ret,
        "mae": worst_ret,
        "exit_reason": exit_reason,
        "okx_ret1": float(signal_row.get("okx_ret1", math.nan)),
        "equity_ret1": float(signal_row.get("equity_ret1", math.nan)),
        "dislocation": float(signal_row.get("dislocation", math.nan)),
    }


def directional_return(side: str, entry: float, price: float) -> float:
    raw = price / entry - 1.0
    return raw if side == "long" else -raw


def summarize(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not trades:
        return []
    df = pd.DataFrame(trades)
    rows: list[dict[str, Any]] = []
    for module, g in df.groupby("module"):
        ret = g["net_return"].astype(float)
        rows.append(
            {
                "module": module,
                "trades": int(len(g)),
                "win_rate": float((ret > 0).mean()),
                "sum_net_return": float(ret.sum()),
                "avg_net_return": float(ret.mean()),
                "median_net_return": float(ret.median()),
                "sharpe_like": float(ret.mean() / ret.std() * math.sqrt(252)) if len(ret) > 2 and float(ret.std()) > 0 else math.nan,
                "avg_mfe": float(g["mfe"].mean()),
                "avg_mae": float(g["mae"].mean()),
                "protected_stop_rate": float((g["exit_reason"] == "protected_stop").mean()),
                "target_rate": float((g["exit_reason"] == "target").mean()),
                "hard_stop_rate": float(g["exit_reason"].isin(["hard_stop", "stop_first_conservative"]).mean()),
            }
        )
    return sorted(rows, key=lambda item: item["sum_net_return"], reverse=True)


def markdown_report(payload: dict[str, Any]) -> str:
    lines = ["# OKX Stock Token Module Backtest", "", f"Generated: {payload['generated_at']}", ""]
    lines.append("| Module | Trades | Win | Sum Ret | Avg Ret | Sharpe-like | Avg MFE | Avg MAE | Target | Hard Stop |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for row in payload["summary"]:
        lines.append(
            "| {module} | {trades} | {win_rate:.1%} | {sum_net_return:.2%} | {avg_net_return:.2%} | {sharpe_like:.2f} | {avg_mfe:.2%} | {avg_mae:.2%} | {target_rate:.1%} | {hard_stop_rate:.1%} |".format(
                **row
            )
        )
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
