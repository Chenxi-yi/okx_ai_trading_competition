#!/usr/bin/env python3
"""Research 20-day Donchian breakout entries.

Signal idea:
- close above the prior N-day high -> long
- close below the prior N-day low -> short

The backtest uses daily bars, confirms on daily close, and enters at the next
daily open to avoid look-ahead bias.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "engine" / "data"
CACHE_DIR = DATA_DIR / "cache"
FEATURE_DIR = DATA_DIR / "features"
OUT_ROOT = DATA_DIR / "research" / "donchian_breakout"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Research N-day Donchian breakout entries")
    p.add_argument("--dataset-id", default="c_auto_feature_store_rebuild_161_ohlcv_snapshot_v1")
    p.add_argument("--symbol-source", choices=["feature", "cache"], default="cache")
    p.add_argument("--start", default="2025-01-01")
    p.add_argument("--end", default=None)
    p.add_argument("--recent-start", default="2026-04-01")
    p.add_argument("--max-symbols", type=int, default=100)
    p.add_argument("--min-volume-usd", type=float, default=200_000.0)
    p.add_argument("--min-listing-days", type=float, default=80.0)
    p.add_argument("--lookback-days", type=int, default=20)
    p.add_argument("--breakout-buffer-pct", type=float, default=0.0)
    p.add_argument("--trend-filter", choices=["none", "sma60", "sma120"], default="none")
    p.add_argument("--volume-filter", choices=["none", "above_sma20"], default="none")
    p.add_argument("--atr-window", type=int, default=14)
    p.add_argument("--stop-atr", type=float, default=2.0)
    p.add_argument("--target-r", type=float, default=2.0)
    p.add_argument("--max-hold-days", type=int, default=10)
    p.add_argument("--cooldown-days", type=int, default=3)
    p.add_argument("--fee-bps-per-side", type=float, default=5.0)
    p.add_argument("--slippage-bps-per-side", type=float, default=2.0)
    p.add_argument("--out-id", default=None)
    p.add_argument("--sweep", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    symbols = _select_symbols(args)
    if args.sweep:
        out_dir = _run_sweep(symbols, args)
        print(json.dumps({"out_dir": str(out_dir.relative_to(ROOT))}, ensure_ascii=False, indent=2))
        return 0
    trades = _research_symbols(symbols, args)
    summary = _summarize(trades, args)
    out_dir = _write_outputs(trades, summary, args)
    print(json.dumps({"out_dir": str(out_dir.relative_to(ROOT)), "summary": summary}, ensure_ascii=False, indent=2))
    return 0


def _run_sweep(symbols: list[str], args: argparse.Namespace) -> Path:
    rows: list[dict[str, Any]] = []
    best: tuple[float, argparse.Namespace, pd.DataFrame, dict[str, Any]] | None = None
    for buffer_pct in (0.0, 0.0025, 0.005, 0.01):
        for trend_filter in ("none", "sma60", "sma120"):
            for volume_filter in ("none", "above_sma20"):
                for stop_atr in (1.5, 2.0, 2.5, 3.0):
                    for target_r in (1.2, 1.6, 2.0, 2.5):
                        for hold_days in (5, 10, 15, 20):
                            cfg = argparse.Namespace(**vars(args))
                            cfg.breakout_buffer_pct = buffer_pct
                            cfg.trend_filter = trend_filter
                            cfg.volume_filter = volume_filter
                            cfg.stop_atr = stop_atr
                            cfg.target_r = target_r
                            cfg.max_hold_days = hold_days
                            trades = _research_symbols(symbols, cfg)
                            summary = _summarize(trades, cfg)
                            recent = _period_summary(trades, str(cfg.recent_start))
                            score = _sweep_score(summary, recent)
                            rows.append(
                                {
                                    "score": score,
                                    "breakout_buffer_pct": buffer_pct,
                                    "trend_filter": trend_filter,
                                    "volume_filter": volume_filter,
                                    "stop_atr": stop_atr,
                                    "target_r": target_r,
                                    "max_hold_days": hold_days,
                                    **{f"summary_{k}": v for k, v in summary.items() if isinstance(v, (int, float, str))},
                                    **{f"recent_{k}": v for k, v in recent.items()},
                                }
                            )
                            if best is None or score > best[0]:
                                best = (score, cfg, trades, summary)
    out_id = args.out_id or f"donchian_breakout_sweep_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    out_dir = OUT_ROOT / out_id
    out_dir.mkdir(parents=True, exist_ok=True)
    results = pd.DataFrame(rows).sort_values("score", ascending=False)
    results.to_csv(out_dir / "sweep_results.csv", index=False)
    results.head(25).to_csv(out_dir / "top25.csv", index=False)
    payload: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "args": vars(args),
        "symbols": len(symbols),
        "configs_tested": len(rows),
        "top": results.head(10).to_dict(orient="records"),
    }
    if best is not None:
        _, cfg, trades, summary = best
        payload["best_config"] = {
            key: getattr(cfg, key)
            for key in ("lookback_days", "breakout_buffer_pct", "trend_filter", "volume_filter", "stop_atr", "target_r", "max_hold_days")
        }
        payload["best_summary"] = summary
        payload["best_recent"] = _period_summary(trades, str(cfg.recent_start))
        if not trades.empty:
            trades.to_csv(out_dir / "best_trades.csv", index=False)
    (out_dir / "summary.json").write_text(json.dumps(payload, indent=2, sort_keys=True))
    (out_dir / "report.md").write_text(_sweep_markdown(payload))
    return out_dir


def _select_symbols(args: argparse.Namespace) -> list[str]:
    available = {p.name.replace("_futures_1d.parquet", "") for p in CACHE_DIR.glob("*_futures_1d.parquet")}
    if str(args.symbol_source) == "cache":
        return sorted(symbol for symbol in available if symbol != "BTC_USDT")[: int(args.max_symbols)]
    path = FEATURE_DIR / args.dataset_id / "features.parquet"
    if not path.exists():
        return sorted(available)[: int(args.max_symbols)]
    cols = ["volume_usd", "listing_age_days", "train_eligible_90d"]
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
        df = _load_daily(symbol, args)
        min_bars = max(int(args.lookback_days) + int(args.max_hold_days) + 5, 80)
        if len(df) < min_bars:
            continue
        rows.extend(_research_symbol(symbol, df, args))
    return pd.DataFrame(rows)


def _load_daily(symbol: str, args: argparse.Namespace) -> pd.DataFrame:
    path = CACHE_DIR / f"{symbol}_futures_1d.parquet"
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


def _research_symbol(symbol: str, df: pd.DataFrame, args: argparse.Namespace) -> list[dict[str, Any]]:
    lookback = int(args.lookback_days)
    channel_high = df["high"].rolling(lookback, min_periods=lookback).max().shift(1)
    channel_low = df["low"].rolling(lookback, min_periods=lookback).min().shift(1)
    atr_abs = _atr_abs(df, int(args.atr_window))
    sma60 = df["close"].rolling(60, min_periods=40).mean()
    sma120 = df["close"].rolling(120, min_periods=80).mean()
    volume_sma20 = df["volume"].rolling(20, min_periods=12).mean()
    trades: list[dict[str, Any]] = []
    last_exit_idx = -10_000
    for i in range(lookback + 2, len(df) - 2):
        if i <= last_exit_idx + int(args.cooldown_days):
            continue
        close = float(df["close"].iloc[i])
        high_level = float(channel_high.iloc[i] or math.nan)
        low_level = float(channel_low.iloc[i] or math.nan)
        if not math.isfinite(high_level) or not math.isfinite(low_level) or high_level <= low_level:
            continue
        side = ""
        buffer = float(args.breakout_buffer_pct)
        if close > high_level * (1.0 + buffer):
            side = "long"
        elif close < low_level * (1.0 - buffer):
            side = "short"
        if not side:
            continue
        if not _filters_pass(df, i, side, sma60, sma120, volume_sma20, args):
            continue
        trade = _simulate_trade(symbol, df, atr_abs, channel_high, channel_low, i, side, args)
        if trade:
            trades.append(trade)
            last_exit_idx = int(trade["exit_idx"])
    return trades


def _filters_pass(
    df: pd.DataFrame,
    i: int,
    side: str,
    sma60: pd.Series,
    sma120: pd.Series,
    volume_sma20: pd.Series,
    args: argparse.Namespace,
) -> bool:
    close = float(df["close"].iloc[i])
    if str(args.volume_filter) == "above_sma20":
        vol_ma = float(volume_sma20.iloc[i] or math.nan)
        if not math.isfinite(vol_ma) or float(df["volume"].iloc[i]) < vol_ma:
            return False
    if str(args.trend_filter) == "sma60":
        ma = float(sma60.iloc[i] or math.nan)
    elif str(args.trend_filter) == "sma120":
        ma = float(sma120.iloc[i] or math.nan)
    else:
        return True
    if not math.isfinite(ma) or ma <= 0:
        return False
    return close > ma if side == "long" else close < ma


def _simulate_trade(
    symbol: str,
    df: pd.DataFrame,
    atr_abs: pd.Series,
    channel_high: pd.Series,
    channel_low: pd.Series,
    signal_idx: int,
    side: str,
    args: argparse.Namespace,
) -> dict[str, Any] | None:
    entry_idx = signal_idx + 1
    entry = float(df["open"].iloc[entry_idx])
    atr = float(atr_abs.iloc[signal_idx] or math.nan)
    if not math.isfinite(atr) or atr <= 0 or entry <= 0:
        return None
    stop_distance = float(args.stop_atr) * atr
    if side == "long":
        stop = entry - stop_distance
        target = entry + float(args.target_r) * stop_distance
        if stop <= 0:
            return None
    else:
        stop = entry + stop_distance
        target = entry - float(args.target_r) * stop_distance
        if target <= 0:
            return None
    risk_pct = abs(entry / stop - 1.0)
    if risk_pct < 0.004 or risk_pct > 0.12:
        return None
    max_exit_idx = min(entry_idx + int(args.max_hold_days), len(df) - 1)
    exit_idx = max_exit_idx
    exit_price = float(df["close"].iloc[exit_idx])
    reason = "horizon"
    for j in range(entry_idx, max_exit_idx + 1):
        high = float(df["high"].iloc[j])
        low = float(df["low"].iloc[j])
        if side == "long":
            if low <= stop:
                exit_idx = j
                exit_price = stop
                reason = "stop"
                break
            if high >= target:
                exit_idx = j
                exit_price = target
                reason = "target"
                break
        else:
            if high >= stop:
                exit_idx = j
                exit_price = stop
                reason = "stop"
                break
            if low <= target:
                exit_idx = j
                exit_price = target
                reason = "target"
                break
    gross = exit_price / entry - 1.0 if side == "long" else entry / exit_price - 1.0
    cost = 2.0 * (float(args.fee_bps_per_side) + float(args.slippage_bps_per_side)) / 10000.0
    net = gross - cost
    return {
        "symbol": symbol.replace("_USDT", "/USDT"),
        "side": side,
        "signal_ts": df.index[signal_idx].isoformat(),
        "entry_ts": df.index[entry_idx].isoformat(),
        "exit_ts": df.index[exit_idx].isoformat(),
        "exit_idx": exit_idx,
        "entry": entry,
        "exit": exit_price,
        "stop": stop,
        "target": target,
        "channel_high": float(channel_high.iloc[signal_idx]),
        "channel_low": float(channel_low.iloc[signal_idx]),
        "exit_reason": reason,
        "gross_return": gross,
        "net_return": net,
        "risk_pct": risk_pct,
        "r_multiple": net / risk_pct if risk_pct else math.nan,
    }


def _atr_abs(df: pd.DataFrame, window: int) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(window, min_periods=max(5, window // 2)).mean()


def _summarize(trades: pd.DataFrame, args: argparse.Namespace) -> dict[str, Any]:
    if trades.empty:
        return {"trades": 0, "verdict": "NO_SIGNAL"}
    ret = pd.to_numeric(trades["net_return"], errors="coerce").dropna()
    wins = ret > 0
    mean = float(ret.mean())
    std = float(ret.std()) if len(ret) > 1 else math.nan
    by_side = {}
    for side, group in trades.groupby("side"):
        r = pd.to_numeric(group["net_return"], errors="coerce").dropna()
        by_side[str(side)] = {
            "trades": int(len(r)),
            "win_rate": float((r > 0).mean()) if len(r) else math.nan,
            "mean_net_return": float(r.mean()) if len(r) else math.nan,
            "sum_net_return": float(r.sum()) if len(r) else 0.0,
        }
    pos_month = _monthly_positive_rate(trades)
    sharpe_like = float(mean / std * math.sqrt(365 / max(1, int(args.max_hold_days)))) if std and std > 0 else math.nan
    verdict = "ROBUST" if mean > 0 and float(wins.mean()) >= 0.50 and pos_month >= 0.58 else "MARGINAL" if mean > 0 and pos_month >= 0.50 else "RANDOM"
    return {
        "trades": int(len(ret)),
        "symbols": int(trades["symbol"].nunique()),
        "win_rate": float(wins.mean()),
        "mean_net_return": mean,
        "median_net_return": float(ret.median()),
        "sum_net_return": float(ret.sum()),
        "sharpe_like": sharpe_like,
        "positive_month_rate": pos_month,
        "target_rate": float((trades["exit_reason"] == "target").mean()),
        "stop_rate": float((trades["exit_reason"] == "stop").mean()),
        "horizon_rate": float((trades["exit_reason"] == "horizon").mean()),
        "avg_risk_pct": float(pd.to_numeric(trades["risk_pct"], errors="coerce").mean()),
        "by_side": by_side,
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


def _sweep_score(summary: dict[str, Any], recent: dict[str, Any]) -> float:
    trades = int(summary.get("trades") or 0)
    if trades < 50:
        return -1e9
    mean = float(summary.get("mean_net_return") or 0.0)
    win = float(summary.get("win_rate") or 0.0)
    pos_month = float(summary.get("positive_month_rate") or 0.0)
    recent_trades = int(recent.get("trades") or 0)
    recent_mean = float(recent.get("mean_net_return") or 0.0) if recent_trades >= 8 else -0.004
    recent_win = float(recent.get("win_rate") or 0.0) if recent_trades >= 8 else 0.0
    stop_rate = float(summary.get("stop_rate") or 0.0)
    return mean * 100.0 + recent_mean * 160.0 + (win - 0.48) * 1.6 + (recent_win - 0.48) * 2.0 + (pos_month - 0.5) * 1.2 - stop_rate * 0.25


def _write_outputs(trades: pd.DataFrame, summary: dict[str, Any], args: argparse.Namespace) -> Path:
    out_id = args.out_id or f"donchian_breakout_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
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
    (out_dir / "report.md").write_text(_single_markdown(payload))
    return out_dir


def _single_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    args = payload["args"]
    return "\n".join(
        [
            "# Donchian Breakout Research",
            "",
            f"- generated_at: `{payload['generated_at']}`",
            f"- lookback_days: `{args['lookback_days']}`",
            f"- breakout_buffer_pct: `{args['breakout_buffer_pct']}`",
            f"- trend_filter: `{args['trend_filter']}`",
            f"- volume_filter: `{args['volume_filter']}`",
            f"- stop_atr: `{args['stop_atr']}`",
            f"- target_r: `{args['target_r']}`",
            f"- max_hold_days: `{args['max_hold_days']}`",
            "",
            "## Summary",
            "",
            f"- trades: `{summary.get('trades')}`",
            f"- win_rate: `{_fmt_pct(summary.get('win_rate'))}`",
            f"- mean_net_return: `{_fmt_pct(summary.get('mean_net_return'))}`",
            f"- recent: `{summary.get('recent')}`",
            f"- verdict: `{summary.get('verdict')}`",
        ]
    )


def _sweep_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Donchian Breakout Sweep",
        "",
        f"- generated_at: `{payload['generated_at']}`",
        f"- symbols: `{payload['symbols']}`",
        f"- configs_tested: `{payload['configs_tested']}`",
        "",
        "## Best",
        "",
        f"- config: `{payload.get('best_config')}`",
        f"- summary: `{payload.get('best_summary')}`",
        f"- recent: `{payload.get('best_recent')}`",
        "",
        "## Top 10",
        "",
        "| rank | score | config | trades | win | mean net | recent trades | recent mean |",
        "|---:|---:|---|---:|---:|---:|---:|---:|",
    ]
    for idx, row in enumerate(payload.get("top", []), 1):
        cfg = f"buf={row.get('breakout_buffer_pct')}, trend={row.get('trend_filter')}, vol={row.get('volume_filter')}, stop={row.get('stop_atr')}, r={row.get('target_r')}, hold={row.get('max_hold_days')}"
        lines.append(
            f"| {idx} | {float(row.get('score') or 0):.3f} | {cfg} | {int(row.get('summary_trades') or 0)} | {_fmt_pct(row.get('summary_win_rate'))} | {_fmt_pct(row.get('summary_mean_net_return'))} | {int(row.get('recent_trades') or 0)} | {_fmt_pct(row.get('recent_mean_net_return'))} |"
        )
    return "\n".join(lines)


def _fmt_pct(value: Any) -> str:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return "nan"
    if not math.isfinite(x):
        return "nan"
    return f"{x * 100:.2f}%"


if __name__ == "__main__":
    raise SystemExit(main())
