#!/usr/bin/env python3
"""Research 4h Fibonacci-support rebound entries.

The idea: use 4h structure to estimate retracement supports, then enter long
on the 1h chart only after the support is touched and reclaimed.
"""

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
DATA_DIR = ROOT / "engine" / "data"
CACHE_DIR = DATA_DIR / "cache"
FEATURE_DIR = DATA_DIR / "features"
OUT_ROOT = DATA_DIR / "research" / "fibonacci_support_rebound"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Research Fibonacci-support rebound entries")
    p.add_argument("--dataset-id", default="c_auto_feature_store_rebuild_161_ohlcv_snapshot_v1")
    p.add_argument("--symbol-source", choices=["feature", "cache"], default="feature")
    p.add_argument("--entry-timeframe", choices=["1h", "4h"], default="1h")
    p.add_argument("--start", default="2025-01-01")
    p.add_argument("--end", default=None)
    p.add_argument("--max-symbols", type=int, default=80)
    p.add_argument("--min-volume-usd", type=float, default=200_000.0)
    p.add_argument("--min-listing-days", type=float, default=60.0)
    p.add_argument("--lookback-4h-bars", type=int, default=42)
    p.add_argument("--min-impulse-pct", type=float, default=0.08)
    p.add_argument("--trend-sma-4h", type=int, default=30)
    p.add_argument("--ratios", default="0.382,0.5,0.618,0.786")
    p.add_argument("--support-tolerance-pct", type=float, default=0.006)
    p.add_argument("--max-pierce-pct", type=float, default=0.012)
    p.add_argument("--confirm-mode", choices=["reclaim", "strong"], default="reclaim")
    p.add_argument("--min-reclaim-pct", type=float, default=0.001)
    p.add_argument("--max-distance-from-support-pct", type=float, default=0.012)
    p.add_argument("--target-r", type=float, default=1.8)
    p.add_argument("--min-target-pct", type=float, default=0.018)
    p.add_argument("--max-target-pct", type=float, default=0.045)
    p.add_argument("--stop-buffer-pct", type=float, default=0.006)
    p.add_argument("--max-hold-hours", type=int, default=18)
    p.add_argument("--cooldown-hours", type=int, default=8)
    p.add_argument("--fee-bps-per-side", type=float, default=5.0)
    p.add_argument("--slippage-bps-per-side", type=float, default=2.0)
    p.add_argument("--recent-start", default="2026-04-01")
    p.add_argument("--out-id", default=None)
    p.add_argument("--sweep", action="store_true")
    p.add_argument("--sweep-depth", choices=["quick", "full"], default="quick")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.sweep:
        out_dir = _run_sweep(args)
        print(json.dumps({"out_dir": str(out_dir.relative_to(ROOT))}, ensure_ascii=False, indent=2))
        return 0
    symbols = _select_symbols(args)
    trades = _research_symbols(symbols, args)
    summary = _summarize(trades, args)
    out_dir = _write_outputs(trades, summary, args)
    print(json.dumps({"out_dir": str(out_dir.relative_to(ROOT)), "summary": summary}, ensure_ascii=False, indent=2))
    return 0


def _run_sweep(args: argparse.Namespace) -> Path:
    if args.sweep_depth == "full":
        ratios_list = ("0.382,0.5,0.618", "0.5,0.618,0.786", "0.382,0.618,0.786")
        tolerances = (0.003, 0.006, 0.009)
        pierces = (0.008, 0.012, 0.018)
        impulses = (0.05, 0.08, 0.12)
        holds = (12, 18, 24)
        target_rs = (1.4, 1.8, 2.2)
        confirm_modes = ("reclaim", "strong")
    else:
        ratios_list = ("0.382,0.5,0.618", "0.5,0.618,0.786")
        tolerances = (0.004, 0.006)
        pierces = (0.012,)
        impulses = (0.05, 0.08, 0.12)
        holds = (12, 18)
        target_rs = (1.4, 1.8)
        confirm_modes = ("reclaim", "strong")
    symbols = _select_symbols(args)
    rows: list[dict[str, Any]] = []
    best: tuple[float, argparse.Namespace, pd.DataFrame, dict[str, Any]] | None = None
    for ratios in ratios_list:
        for tolerance in tolerances:
            for pierce in pierces:
                for impulse in impulses:
                    for hold in holds:
                        for target_r in target_rs:
                            for confirm_mode in confirm_modes:
                                cfg = argparse.Namespace(**vars(args))
                                cfg.ratios = ratios
                                cfg.support_tolerance_pct = tolerance
                                cfg.max_pierce_pct = pierce
                                cfg.min_impulse_pct = impulse
                                cfg.max_hold_hours = hold
                                cfg.target_r = target_r
                                cfg.confirm_mode = confirm_mode
                                trades = _research_symbols(symbols, cfg)
                                summary = _summarize(trades, cfg)
                                recent = _period_summary(trades, str(cfg.recent_start))
                                score = _sweep_score(trades, summary, recent)
                                row = {
                                    "score": score,
                                    "ratios": ratios,
                                    "support_tolerance_pct": tolerance,
                                    "max_pierce_pct": pierce,
                                    "min_impulse_pct": impulse,
                                    "max_hold_hours": hold,
                                    "target_r": target_r,
                                    "confirm_mode": confirm_mode,
                                    **{f"summary_{k}": v for k, v in summary.items() if isinstance(v, (int, float, str))},
                                    **{f"recent_{k}": v for k, v in recent.items()},
                                }
                                rows.append(row)
                                if best is None or score > best[0]:
                                    best = (score, cfg, trades, summary)
    out_id = args.out_id or f"fib_support_rebound_sweep_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    out_dir = OUT_ROOT / out_id
    out_dir.mkdir(parents=True, exist_ok=True)
    results = pd.DataFrame(rows).sort_values("score", ascending=False)
    results.to_csv(out_dir / "sweep_results.csv", index=False)
    results.head(25).to_csv(out_dir / "top25.csv", index=False)
    payload: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "args": vars(args),
        "configs_tested": len(rows),
        "top": results.head(10).to_dict(orient="records"),
    }
    if best is not None:
        _, cfg, trades, summary = best
        payload["best_config"] = {
            key: getattr(cfg, key)
            for key in (
                "ratios",
                "support_tolerance_pct",
                "max_pierce_pct",
                "min_impulse_pct",
                "max_hold_hours",
                "target_r",
                "confirm_mode",
            )
        }
        payload["best_summary"] = summary
        payload["best_recent"] = _period_summary(trades, str(cfg.recent_start))
        if not trades.empty:
            trades.to_csv(out_dir / "best_trades.csv", index=False)
    (out_dir / "summary.json").write_text(json.dumps(payload, indent=2, sort_keys=True))
    (out_dir / "report.md").write_text(_sweep_markdown(payload))
    return out_dir


def _select_symbols(args: argparse.Namespace) -> list[str]:
    h1 = {p.name.replace("_futures_1h.parquet", "") for p in CACHE_DIR.glob("*_futures_1h.parquet")}
    h4 = {p.name.replace("_futures_4h.parquet", "") for p in CACHE_DIR.glob("*_futures_4h.parquet")}
    available = h4 if str(getattr(args, "entry_timeframe", "1h")) == "4h" else h1 & h4
    if str(getattr(args, "symbol_source", "feature")) == "cache":
        return sorted(symbol for symbol in available if symbol != "BTC_USDT")[: int(args.max_symbols)]
    path = FEATURE_DIR / args.dataset_id / "features.parquet"
    if not path.exists():
        return sorted(available)[: int(args.max_symbols)]
    cols = ["close", "volume_usd", "listing_age_days", "train_eligible_90d"]
    df = pd.read_parquet(path, columns=cols)
    latest_ts = df.index.get_level_values("timestamp").max()
    latest = df.loc[df.index.get_level_values("timestamp") == latest_ts].reset_index()
    latest["safe"] = latest["symbol"].astype(str).str.replace("/", "_", regex=False).str.replace(":", "_", regex=False)
    latest["volume_usd"] = pd.to_numeric(latest["volume_usd"], errors="coerce").fillna(0.0)
    latest["listing_age_days"] = pd.to_numeric(latest["listing_age_days"], errors="coerce").fillna(0.0)
    latest["train_eligible_90d"] = pd.to_numeric(latest["train_eligible_90d"], errors="coerce").fillna(0.0)
    latest = latest[
        (latest["safe"].isin(available))
        & (latest["safe"] != "BTC_USDT")
        & (latest["volume_usd"] >= float(args.min_volume_usd))
        & (latest["listing_age_days"] >= float(args.min_listing_days))
        & (latest["train_eligible_90d"] > 0)
    ].sort_values("volume_usd", ascending=False)
    return latest["safe"].astype(str).head(int(args.max_symbols)).tolist()


def _research_symbols(symbols: list[str], args: argparse.Namespace) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for symbol in symbols:
        h1 = _load_ohlcv(symbol, str(getattr(args, "entry_timeframe", "1h")), args)
        h4 = _load_ohlcv(symbol, "4h", args)
        min_exec_bars = 80 if str(getattr(args, "entry_timeframe", "1h")) == "4h" else 96
        if len(h1) < min_exec_bars or len(h4) < max(80, int(args.lookback_4h_bars) + 20):
            continue
        rows.extend(_research_symbol(symbol, h1, h4, args))
    return pd.DataFrame(rows)


def _load_ohlcv(symbol: str, timeframe: str, args: argparse.Namespace) -> pd.DataFrame:
    path = CACHE_DIR / f"{symbol}_futures_{timeframe}.parquet"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_parquet(path).copy()
    df.index = pd.to_datetime(df.index, utc=True)
    df = df.sort_index()
    start = pd.Timestamp(args.start, tz="UTC")
    end = pd.Timestamp(args.end, tz="UTC") if args.end else pd.Timestamp.now(tz="UTC")
    df = df.loc[(df.index >= start) & (df.index <= end)]
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["open", "high", "low", "close"])


def _research_symbol(symbol: str, h1: pd.DataFrame, h4: pd.DataFrame, args: argparse.Namespace) -> list[dict[str, Any]]:
    h4_state = _h4_fib_state(h4, args)
    h1 = h1.join(h4_state.reindex(h1.index, method="ffill"), how="left")
    ret = h1["close"].pct_change()
    atr = _atr_pct(h1, 14).fillna(ret.abs().rolling(24, min_periods=8).median() * 4.0)
    trades: list[dict[str, Any]] = []
    last_exit_ts: pd.Timestamp | None = None
    for i in range(6, len(h1) - 2):
        now = h1.index[i]
        if last_exit_ts is not None and now < last_exit_ts + pd.Timedelta(hours=int(args.cooldown_hours)):
            continue
        row = h1.iloc[i]
        if not bool(row.get("fib_valid", False)):
            continue
        signal = _support_reclaim_signal(h1.iloc[i - 5 : i + 1], args)
        if not signal:
            continue
        trade = _simulate_trade(symbol, h1, atr, i, signal, args)
        if trade:
            trades.append(trade)
            last_exit_ts = pd.Timestamp(trade["exit_ts"])
    return trades


def _h4_fib_state(h4: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    out = pd.DataFrame(index=h4.index)
    close = h4["close"].astype(float)
    lookback = int(args.lookback_4h_bars)
    high = h4["high"].rolling(lookback, min_periods=max(12, lookback // 2)).max().shift(1)
    low = h4["low"].rolling(lookback, min_periods=max(12, lookback // 2)).min().shift(1)
    impulse = high / low - 1.0
    sma = close.rolling(int(args.trend_sma_4h), min_periods=max(8, int(args.trend_sma_4h) // 2)).mean()
    trend_ok = (close >= sma) & (impulse >= float(args.min_impulse_pct)) & (high > low)
    ratios = _parse_ratios(args.ratios)
    levels = []
    for ratio in ratios:
        col = f"fib_{str(ratio).replace('.', '')}"
        out[col] = high - ratio * (high - low)
        levels.append(col)
    out["fib_valid"] = trend_ok
    out["fib_high"] = high
    out["fib_low"] = low
    out["fib_impulse"] = impulse
    out["fib_level_cols"] = ",".join(levels)
    return out


def _support_reclaim_signal(recent: pd.DataFrame, args: argparse.Namespace) -> dict[str, Any] | None:
    if len(recent) < 6:
        return None
    last = recent.iloc[-1]
    prev = recent.iloc[-2]
    level_cols = [col for col in str(last.get("fib_level_cols") or "").split(",") if col and col in recent.columns]
    if not level_cols:
        return None
    close = float(last["close"])
    low = float(last["low"])
    open_ = float(last["open"])
    if close <= 0 or low <= 0:
        return None
    candidates = []
    for col in level_cols:
        level = float(last.get(col) or math.nan)
        if not math.isfinite(level) or level <= 0:
            continue
        touched = low <= level * (1.0 + float(args.support_tolerance_pct))
        not_broken = low >= level * (1.0 - float(args.max_pierce_pct))
        reclaimed = close >= level * (1.0 + float(args.min_reclaim_pct))
        distance_ok = close / level - 1.0 <= float(args.max_distance_from_support_pct)
        if touched and not_broken and reclaimed and distance_ok:
            candidates.append((abs(close / level - 1.0), col, level))
    if not candidates:
        return None
    _, level_col, level = sorted(candidates)[0]
    bullish_body = close > open_
    regained_prev = close > float(prev["close"])
    broke_prev_high = close > float(prev["high"])
    higher_low = low >= float(prev["low"]) * (1.0 - 0.001)
    if str(args.confirm_mode) == "strong":
        confirmed = bullish_body and (broke_prev_high or (regained_prev and higher_low))
    else:
        confirmed = (bullish_body and regained_prev) or broke_prev_high
    if not confirmed:
        return None
    return {"level_col": level_col, "support": level, "trigger": "fib_support_reclaim_strong" if str(args.confirm_mode) == "strong" else "fib_support_reclaim"}


def _simulate_trade(
    symbol: str,
    h1: pd.DataFrame,
    atr: pd.Series,
    entry_idx: int,
    signal: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any] | None:
    entry_ts = h1.index[entry_idx]
    entry = float(h1["close"].iloc[entry_idx])
    support = float(signal["support"])
    if entry <= 0 or support <= 0:
        return None
    atr_pct = float(atr.iloc[entry_idx] or 0.0)
    stop_buffer = max(float(args.stop_buffer_pct), 0.45 * atr_pct)
    stop = support * (1.0 - stop_buffer)
    risk_pct = entry / stop - 1.0 if stop > 0 else math.nan
    if not math.isfinite(risk_pct) or risk_pct <= 0.004 or risk_pct > 0.045:
        return None
    target_pct = min(float(args.max_target_pct), max(float(args.min_target_pct), float(args.target_r) * risk_pct))
    target = entry * (1.0 + target_pct)
    h4_high = float(h1["fib_high"].iloc[entry_idx] or math.nan)
    if math.isfinite(h4_high) and h4_high > entry:
        target = min(target, h4_high * 0.995)
    if target <= entry * (1.0 + float(args.min_target_pct) * 0.75):
        return None
    hold_bars = _hours_to_bars(int(args.max_hold_hours), str(getattr(args, "entry_timeframe", "1h")))
    exit_ts = h1.index[min(entry_idx + hold_bars, len(h1) - 1)]
    exit_price = float(h1["close"].loc[exit_ts])
    reason = "horizon"
    for j in range(entry_idx + 1, min(entry_idx + hold_bars, len(h1) - 1) + 1):
        bar = h1.iloc[j]
        ts = h1.index[j]
        if float(bar["low"]) <= stop:
            exit_ts = ts
            exit_price = stop
            reason = "stop"
            break
        if float(bar["high"]) >= target:
            exit_ts = ts
            exit_price = target
            reason = "target"
            break
    gross = exit_price / entry - 1.0
    cost = 2.0 * (float(args.fee_bps_per_side) + float(args.slippage_bps_per_side)) / 10000.0
    net = gross - cost
    return {
        "entry_ts": entry_ts.isoformat(),
        "exit_ts": pd.Timestamp(exit_ts).isoformat(),
        "symbol": symbol.replace("_USDT", "/USDT"),
        "side": "long",
        "trigger": str(signal["trigger"]),
        "fib_level": str(signal["level_col"]),
        "support": support,
        "entry": entry,
        "exit": exit_price,
        "stop": stop,
        "target": target,
        "exit_reason": reason,
        "gross_return": gross,
        "net_return": net,
        "r_multiple": net / risk_pct,
        "risk_pct": risk_pct,
        "target_pct": target / entry - 1.0,
        "h4_impulse": float(h1["fib_impulse"].iloc[entry_idx]),
    }


def _atr_pct(df: pd.DataFrame, window: int) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(window, min_periods=max(4, window // 2)).mean()
    return atr / df["close"]


def _hours_to_bars(hours: int, timeframe: str) -> int:
    bar_hours = 4 if timeframe == "4h" else 1
    return max(1, int(math.ceil(hours / bar_hours)))


def _parse_ratios(raw: str) -> list[float]:
    ratios = []
    for item in str(raw).split(","):
        item = item.strip()
        if not item:
            continue
        value = float(item)
        if 0.0 < value < 1.0:
            ratios.append(value)
    if not ratios:
        raise ValueError("ratios must include at least one value between 0 and 1")
    return ratios


def _summarize(trades: pd.DataFrame, args: argparse.Namespace) -> dict[str, Any]:
    if trades.empty:
        return {"trades": 0, "verdict": "NO_SIGNAL"}
    ret = pd.to_numeric(trades["net_return"], errors="coerce").dropna()
    wins = ret > 0
    by_level = {}
    for level, group in trades.groupby("fib_level"):
        r = pd.to_numeric(group["net_return"], errors="coerce").dropna()
        by_level[str(level)] = {
            "trades": int(len(r)),
            "win_rate": float((r > 0).mean()) if len(r) else math.nan,
            "mean_net_return": float(r.mean()) if len(r) else math.nan,
            "total_net_return_units": float(r.sum()) if len(r) else 0.0,
        }
    positive_months = _monthly_positive_rate(trades)
    mean = float(ret.mean()) if len(ret) else math.nan
    std = float(ret.std()) if len(ret) > 1 else math.nan
    sharpe_like = float(mean / std * math.sqrt(365 * 24 / max(1, int(args.max_hold_hours)))) if std and std > 0 else math.nan
    verdict = "ROBUST" if float(wins.mean()) >= 0.54 and mean > 0 and positive_months >= 0.58 else "MARGINAL" if mean > 0 and float(wins.mean()) >= 0.50 else "RANDOM"
    return {
        "trades": int(len(ret)),
        "symbols": int(trades["symbol"].nunique()),
        "win_rate": float(wins.mean()),
        "mean_net_return": mean,
        "median_net_return": float(ret.median()),
        "total_net_return_units": float(ret.sum()),
        "sharpe_like": sharpe_like,
        "positive_month_rate": positive_months,
        "target_rate": float((trades["exit_reason"] == "target").mean()),
        "stop_rate": float((trades["exit_reason"] == "stop").mean()),
        "horizon_rate": float((trades["exit_reason"] == "horizon").mean()),
        "avg_risk_pct": float(pd.to_numeric(trades["risk_pct"], errors="coerce").mean()),
        "by_level": by_level,
        "recent": _period_summary(trades, str(args.recent_start)),
        "verdict": verdict,
    }


def _period_summary(trades: pd.DataFrame, start: str) -> dict[str, Any]:
    if trades.empty:
        return {"trades": 0, "win_rate": math.nan, "mean_net_return": math.nan, "sum_net_return": 0.0}
    sample = trades[pd.to_datetime(trades["entry_ts"], utc=True) >= pd.Timestamp(start, tz="UTC")]
    if sample.empty:
        return {"trades": 0, "win_rate": math.nan, "mean_net_return": math.nan, "sum_net_return": 0.0}
    ret = pd.to_numeric(sample["net_return"], errors="coerce").dropna()
    return {
        "trades": int(len(ret)),
        "win_rate": float((ret > 0).mean()) if len(ret) else math.nan,
        "mean_net_return": float(ret.mean()) if len(ret) else math.nan,
        "sum_net_return": float(ret.sum()) if len(ret) else 0.0,
    }


def _monthly_positive_rate(trades: pd.DataFrame) -> float:
    if trades.empty:
        return math.nan
    df = trades.copy()
    df["month"] = pd.to_datetime(df["entry_ts"], utc=True).dt.strftime("%Y-%m")
    monthly = df.groupby("month")["net_return"].sum()
    return float((monthly > 0).mean()) if len(monthly) else math.nan


def _sweep_score(trades: pd.DataFrame, summary: dict[str, Any], recent: dict[str, Any]) -> float:
    if trades.empty or int(summary.get("trades") or 0) < 60:
        return -1e9
    mean = float(summary.get("mean_net_return") or 0.0)
    win = float(summary.get("win_rate") or 0.0)
    pos_month = float(summary.get("positive_month_rate") or 0.0)
    recent_trades = int(recent.get("trades") or 0)
    recent_mean = float(recent.get("mean_net_return") or 0.0) if recent_trades >= 10 else -0.005
    recent_win = float(recent.get("win_rate") or 0.0) if recent_trades >= 10 else 0.0
    stop_rate = float(summary.get("stop_rate") or 0.0)
    return mean * 120.0 + recent_mean * 180.0 + (win - 0.5) * 2.0 + (recent_win - 0.5) * 2.5 + (pos_month - 0.5) * 1.5 - stop_rate * 0.5


def _write_outputs(trades: pd.DataFrame, summary: dict[str, Any], args: argparse.Namespace) -> Path:
    out_id = args.out_id or f"fib_support_rebound_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    out_dir = OUT_ROOT / out_id
    out_dir.mkdir(parents=True, exist_ok=True)
    if not trades.empty:
        trades.to_csv(out_dir / "trades.csv", index=False)
        trades.tail(500).to_csv(out_dir / "trades_tail.csv", index=False)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "args": vars(args),
        "summary": summary,
    }
    (out_dir / "summary.json").write_text(json.dumps(payload, indent=2, sort_keys=True))
    (out_dir / "report.md").write_text(_markdown_report(payload))
    return out_dir


def _markdown_report(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Fibonacci Support Rebound Research",
        "",
        f"Generated: {payload['generated_at']}",
        "",
        "## Summary",
        "",
    ]
    for key in (
        "trades",
        "symbols",
        "win_rate",
        "mean_net_return",
        "median_net_return",
        "total_net_return_units",
        "positive_month_rate",
        "target_rate",
        "stop_rate",
        "horizon_rate",
        "avg_risk_pct",
        "verdict",
    ):
        lines.append(f"- {key}: {summary.get(key)}")
    lines.append("")
    lines.append("## Recent")
    lines.append("")
    lines.append(str(summary.get("recent")))
    lines.append("")
    lines.append("## By Level")
    lines.append("")
    for level, row in (summary.get("by_level") or {}).items():
        lines.append(f"- {level}: {row}")
    lines.append("")
    return "\n".join(lines)


def _sweep_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Fibonacci Support Rebound Sweep",
        "",
        f"Generated: {payload['generated_at']}",
        f"Configs tested: {payload['configs_tested']}",
        "",
        "## Best Config",
        "",
        "```json",
        json.dumps(payload.get("best_config", {}), indent=2, sort_keys=True),
        "```",
        "",
        "## Best Summary",
        "",
        "```json",
        json.dumps(payload.get("best_summary", {}), indent=2, sort_keys=True),
        "```",
        "",
        "## Best Recent",
        "",
        "```json",
        json.dumps(payload.get("best_recent", {}), indent=2, sort_keys=True),
        "```",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
