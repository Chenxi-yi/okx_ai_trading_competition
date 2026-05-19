#!/usr/bin/env python3
"""Research key-level breakout trades with fixed R:R exits."""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT / "engine" / "data" / "cache"
OUT_ROOT = ROOT / "engine" / "data" / "research" / "key_level_rr_breakout"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backtest key-level breakout with 1:R target")
    p.add_argument("--symbols", default="BTC_USDT,ETH_USDT,SOL_USDT")
    p.add_argument("--timeframe", choices=["4h", "1d"], default="1d")
    p.add_argument("--start", default="2023-01-01")
    p.add_argument("--end", default="2026-05-14")
    p.add_argument("--lookback-bars", type=int, default=20)
    p.add_argument("--breakout-buffer-pct", type=float, default=0.002)
    p.add_argument("--stop-buffer-pct", type=float, default=0.003)
    p.add_argument("--target-r", type=float, default=2.0)
    p.add_argument("--max-hold-bars", type=int, default=20)
    p.add_argument("--cooldown-bars", type=int, default=3)
    p.add_argument("--side-mode", choices=["long", "long_short"], default="long")
    p.add_argument("--trend-filter", choices=["off", "sma"], default="sma")
    p.add_argument("--trend-sma-bars", type=int, default=50)
    p.add_argument("--leverage", type=float, default=1.0)
    p.add_argument("--risk-fraction", type=float, default=0.25)
    p.add_argument("--fee-bps-per-side", type=float, default=5.0)
    p.add_argument("--slippage-bps-per-side", type=float, default=2.0)
    p.add_argument("--out-id", default=None)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    trades = []
    for symbol in [s.strip() for s in str(args.symbols).split(",") if s.strip()]:
        df = _load_ohlcv(symbol, args)
        if not df.empty:
            trades.extend(_research_symbol(symbol, df, args))
    trades_df = pd.DataFrame(trades)
    equity = _equity_curve(trades_df, args)
    summary = _summary(trades_df, equity, args)
    out_dir = OUT_ROOT / (args.out_id or f"key_level_rr_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}")
    out_dir.mkdir(parents=True, exist_ok=True)
    trades_df.to_csv(out_dir / "trades.csv", index=False)
    equity.to_csv(out_dir / "equity_curve.csv", index=False)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
    print(json.dumps({"out_dir": str(out_dir.relative_to(ROOT)), "summary": summary}, indent=2, sort_keys=True))
    return 0


def _load_ohlcv(symbol: str, args: argparse.Namespace) -> pd.DataFrame:
    path = CACHE_DIR / f"{symbol}_futures_{args.timeframe}.parquet"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_parquet(path).copy()
    df.index = pd.to_datetime(df.index, utc=True)
    df = df.sort_index()
    start = pd.Timestamp(args.start, tz="UTC")
    end = pd.Timestamp(args.end, tz="UTC")
    df = df.loc[(df.index >= start) & (df.index <= end)]
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["open", "high", "low", "close"])


def _research_symbol(symbol: str, df: pd.DataFrame, args: argparse.Namespace) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    lookback = int(args.lookback_bars)
    prior_high = df["high"].rolling(lookback, min_periods=max(5, lookback // 2)).max().shift(1)
    prior_low = df["low"].rolling(lookback, min_periods=max(5, lookback // 2)).min().shift(1)
    sma = df["close"].rolling(int(args.trend_sma_bars), min_periods=max(8, int(args.trend_sma_bars) // 2)).mean()
    last_exit_idx = -10**9
    for i in range(lookback + 1, len(df) - 2):
        if i <= last_exit_idx + int(args.cooldown_bars):
            continue
        close = float(df["close"].iloc[i])
        ph = float(prior_high.iloc[i])
        pl = float(prior_low.iloc[i])
        if not all(math.isfinite(x) and x > 0 for x in (close, ph, pl)):
            continue
        long_ok = close > ph * (1.0 + float(args.breakout_buffer_pct))
        short_ok = close < pl * (1.0 - float(args.breakout_buffer_pct))
        if str(args.trend_filter) == "sma":
            trend = float(sma.iloc[i])
            long_ok &= math.isfinite(trend) and close > trend
            short_ok &= math.isfinite(trend) and close < trend
        side = ""
        level = math.nan
        if long_ok:
            side, level = "long", ph
        elif str(args.side_mode) == "long_short" and short_ok:
            side, level = "short", pl
        if not side:
            continue
        entry_idx = i + 1
        entry = float(df["open"].iloc[entry_idx])
        if side == "long":
            stop = level * (1.0 - float(args.stop_buffer_pct))
            risk = entry - stop
            target = entry + risk * float(args.target_r)
        else:
            stop = level * (1.0 + float(args.stop_buffer_pct))
            risk = stop - entry
            target = entry - risk * float(args.target_r)
        if not math.isfinite(risk) or risk <= entry * 0.001 or risk > entry * 0.15:
            continue
        trade = _simulate_trade(symbol, df, entry_idx, side, level, entry, stop, target, args)
        if trade:
            out.append(trade)
            last_exit_idx = int(trade["exit_idx"])
    return out


def _simulate_trade(
    symbol: str,
    df: pd.DataFrame,
    entry_idx: int,
    side: str,
    level: float,
    entry: float,
    stop: float,
    target: float,
    args: argparse.Namespace,
) -> dict[str, Any] | None:
    max_i = min(len(df) - 1, entry_idx + int(args.max_hold_bars))
    exit_idx = max_i
    exit_price = float(df["close"].iloc[max_i])
    reason = "horizon"
    for j in range(entry_idx, max_i + 1):
        high = float(df["high"].iloc[j])
        low = float(df["low"].iloc[j])
        if side == "long":
            stop_hit = low <= stop
            target_hit = high >= target
        else:
            stop_hit = high >= stop
            target_hit = low <= target
        if stop_hit or target_hit:
            exit_idx = j
            if stop_hit:
                exit_price = stop
                reason = "stop"
            else:
                exit_price = target
                reason = "target"
            break
    gross = exit_price / entry - 1.0
    if side == "short":
        gross = -gross
    cost = 2.0 * (float(args.fee_bps_per_side) + float(args.slippage_bps_per_side)) / 10000.0
    net = gross - cost
    risk_pct = abs(entry - stop) / entry
    return {
        "entry_ts": df.index[entry_idx].isoformat(),
        "exit_ts": df.index[exit_idx].isoformat(),
        "entry_idx": entry_idx,
        "exit_idx": exit_idx,
        "symbol": symbol,
        "side": side,
        "level": level,
        "entry": entry,
        "stop": stop,
        "target": target,
        "exit": exit_price,
        "exit_reason": reason,
        "gross_return": gross,
        "net_return": net,
        "risk_pct": risk_pct,
        "r_multiple": net / risk_pct if risk_pct > 0 else math.nan,
        "hold_bars": exit_idx - entry_idx,
    }


def _equity_curve(trades: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    nav = 100.0
    rows = []
    if trades.empty:
        return pd.DataFrame([{"ts": args.start, "nav": nav}])
    trades = trades.sort_values("exit_ts")
    for _, row in trades.iterrows():
        pnl_ret = float(row["net_return"]) * float(args.leverage) * float(args.risk_fraction)
        nav *= 1.0 + pnl_ret
        rows.append({"ts": row["exit_ts"], "nav": nav})
    return pd.DataFrame(rows)


def _summary(trades: pd.DataFrame, equity: pd.DataFrame, args: argparse.Namespace) -> dict[str, Any]:
    if trades.empty:
        return {"trades": 0, "args": vars(args)}
    ret = pd.to_numeric(trades["net_return"], errors="coerce")
    eq = equity.copy()
    eq["ts"] = pd.to_datetime(eq["ts"], utc=True)
    nav = pd.to_numeric(eq["nav"], errors="coerce")
    start = pd.Timestamp(args.start, tz="UTC")
    end = pd.Timestamp(args.end, tz="UTC")
    years = max(1e-9, (end - start).days / 365.0)
    total = float(nav.iloc[-1] / 100.0 - 1.0)
    peak = nav.cummax()
    dd = nav / peak - 1.0
    by_symbol = {}
    for symbol, group in trades.groupby("symbol"):
        r = pd.to_numeric(group["net_return"], errors="coerce")
        by_symbol[str(symbol)] = {"trades": int(len(group)), "win_rate": float((r > 0).mean()), "avg_net_return": float(r.mean())}
    return {
        "args": vars(args),
        "trades": int(len(trades)),
        "win_rate": float((ret > 0).mean()),
        "avg_net_return": float(ret.mean()),
        "median_net_return": float(ret.median()),
        "avg_r_multiple": float(pd.to_numeric(trades["r_multiple"], errors="coerce").mean()),
        "target_rate": float((trades["exit_reason"] == "target").mean()),
        "stop_rate": float((trades["exit_reason"] == "stop").mean()),
        "horizon_rate": float((trades["exit_reason"] == "horizon").mean()),
        "final_nav": float(nav.iloc[-1]),
        "total_return": total,
        "annualized_return": float((1.0 + total) ** (1.0 / years) - 1.0) if total > -1 else -1.0,
        "max_drawdown": float(dd.min()),
        "by_symbol": by_symbol,
    }


if __name__ == "__main__":
    raise SystemExit(main())
