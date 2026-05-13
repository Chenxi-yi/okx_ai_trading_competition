#!/usr/bin/env python3
"""Research 4h-trend / 1h-pullback reversal entries.

The idea: trade with the 4h trend, but enter on the 1h chart after a controlled
countertrend pullback shows a reversal trigger such as engulfing or a fractal.
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
OUT_ROOT = DATA_DIR / "research" / "trend_pullback_reversal"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Research trend-pullback reversal entries")
    p.add_argument("--dataset-id", default="c_auto_feature_store_rebuild_161_ohlcv_snapshot_v1")
    p.add_argument("--start", default="2025-01-01")
    p.add_argument("--end", default=None)
    p.add_argument("--max-symbols", type=int, default=80)
    p.add_argument("--min-volume-usd", type=float, default=200_000.0)
    p.add_argument("--min-listing-days", type=float, default=60.0)
    p.add_argument("--trend-sma-bars", type=int, default=12)
    p.add_argument("--trend-lookback-bars", type=int, default=6)
    p.add_argument("--pullback-bars", type=int, default=4)
    p.add_argument("--max-countertrend-multiple", type=float, default=4.0)
    p.add_argument("--max-countertrend-move-pct", type=float, default=0.045)
    p.add_argument("--h4-trend-min", type=float, default=0.0)
    p.add_argument("--h4-countertrend-allow", type=float, default=0.005)
    p.add_argument("--near-extreme-pct", type=float, default=0.003)
    p.add_argument("--loose-extreme-pct", type=float, default=0.006)
    p.add_argument("--trigger-range-frac", type=float, default=0.25)
    p.add_argument("--side-mode", choices=["both", "long", "short"], default="both")
    p.add_argument("--regime-allowlist", default="", help="Comma-separated btc_regime_6 values to allow")
    p.add_argument("--short-decay-gate", choices=["off", "loose", "strict"], default="off")
    p.add_argument("--short-decay-min-frac", type=float, default=0.25)
    p.add_argument("--short-max-bounce-pct", type=float, default=0.03)
    p.add_argument("--target-pct", type=float, default=0.03)
    p.add_argument("--stop-pct", type=float, default=0.015)
    p.add_argument("--max-hold-hours", type=int, default=12)
    p.add_argument("--fee-bps-per-side", type=float, default=5.0)
    p.add_argument("--slippage-bps-per-side", type=float, default=2.0)
    p.add_argument("--out-id", default=None)
    p.add_argument("--mode", choices=["feature_proxy", "ohlcv_path"], default="feature_proxy")
    p.add_argument("--sweep", action="store_true", help="Run a conservative parameter sweep for feature_proxy mode")
    p.add_argument("--sweep-depth", choices=["quick", "full"], default="quick")
    p.add_argument("--recent-start", default="2026-04-01")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.sweep:
        out_dir = _run_sweep(args)
        print(json.dumps({"out_dir": str(out_dir.relative_to(ROOT))}, ensure_ascii=False, indent=2))
        return 0
    if args.mode == "feature_proxy":
        rows = _research_feature_proxy(args)
    else:
        symbols = _select_symbols(args)
        rows = []
        for symbol in symbols:
            h1 = _load_ohlcv(symbol, "1h", args)
            h4 = _load_ohlcv(symbol, "4h", args)
            if len(h1) < 200 or len(h4) < 80:
                continue
            rows.extend(_research_symbol(symbol, h1, h4, args))
    trades = pd.DataFrame(rows)
    summary = _summarize(trades, args)
    out_dir = _write_outputs(trades, summary, args)
    print(json.dumps({"out_dir": str(out_dir.relative_to(ROOT)), "summary": summary}, ensure_ascii=False, indent=2))
    return 0


def _run_sweep(args: argparse.Namespace) -> Path:
    base = _prepare_feature_proxy_frame(args)
    configs = []
    if args.sweep_depth == "full":
        side_modes = ("both", "long", "short")
        h4_trend_mins = (0.0, 0.006, 0.012)
        h4_allows = (0.0025, 0.005, 0.01)
        nears = (0.0015, 0.003, 0.006)
        multiples = (3.0, 4.0, 6.0)
        holds = (6, 8, 12)
    else:
        side_modes = ("long", "short")
        h4_trend_mins = (0.0, 0.006, 0.012)
        h4_allows = (0.005,)
        nears = (0.0015, 0.003)
        multiples = (4.0,)
        holds = (6, 12)
    for side_mode in side_modes:
        for h4_trend_min in h4_trend_mins:
            for h4_allow in h4_allows:
                for near in nears:
                    for multiple in multiples:
                        for hold in holds:
                            cfg = vars(args).copy()
                            cfg.update(
                                {
                                    "side_mode": side_mode,
                                    "h4_trend_min": h4_trend_min,
                                    "h4_countertrend_allow": h4_allow,
                                    "near_extreme_pct": near,
                                    "loose_extreme_pct": near * 2.0,
                                    "trigger_range_frac": 0.25,
                                    "max_countertrend_multiple": multiple,
                                    "max_hold_hours": hold,
                                    "short_decay_gate": "strict" if side_mode == "short" else str(getattr(args, "short_decay_gate", "off")),
                                }
                            )
                            configs.append(argparse.Namespace(**cfg))
    rows = []
    best: tuple[float, argparse.Namespace, pd.DataFrame, dict[str, Any]] | None = None
    for cfg in configs:
        trades = pd.DataFrame(_research_feature_proxy(cfg, base))
        summary = _summarize(trades, cfg)
        score = _sweep_score(trades, summary, cfg)
        row = {
            "score": score,
            "side_mode": cfg.side_mode,
            "h4_trend_min": cfg.h4_trend_min,
            "h4_countertrend_allow": cfg.h4_countertrend_allow,
            "near_extreme_pct": cfg.near_extreme_pct,
            "max_countertrend_multiple": cfg.max_countertrend_multiple,
            "max_hold_hours": cfg.max_hold_hours,
            **{f"summary_{k}": v for k, v in summary.items() if isinstance(v, (int, float, str))},
        }
        recent = _period_summary(trades, str(cfg.recent_start))
        row.update({f"recent_{k}": v for k, v in recent.items()})
        rows.append(row)
        if best is None or score > best[0]:
            best = (score, cfg, trades, summary)
    out_id = args.out_id or f"trend_pullback_reversal_sweep_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    out_dir = OUT_ROOT / out_id
    out_dir.mkdir(parents=True, exist_ok=True)
    results = pd.DataFrame(rows).sort_values("score", ascending=False)
    results.to_csv(out_dir / "sweep_results.csv", index=False)
    results.head(25).to_csv(out_dir / "top25.csv", index=False)
    payload: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "args": vars(args),
        "configs_tested": len(configs),
        "top": results.head(10).to_dict(orient="records"),
    }
    if best is not None:
        _, cfg, trades, summary = best
        if not trades.empty:
            trades.to_csv(out_dir / "best_trades.csv", index=False)
        payload["best_config"] = {k: getattr(cfg, k) for k in ("side_mode", "h4_trend_min", "h4_countertrend_allow", "near_extreme_pct", "loose_extreme_pct", "max_countertrend_multiple", "max_hold_hours", "target_pct", "stop_pct")}
        payload["best_summary"] = summary
        payload["best_recent"] = _period_summary(trades, str(cfg.recent_start))
    (out_dir / "summary.json").write_text(json.dumps(payload, indent=2, sort_keys=True))
    (out_dir / "report.md").write_text(_sweep_markdown(payload))
    return out_dir


def _sweep_score(trades: pd.DataFrame, summary: dict[str, Any], args: argparse.Namespace) -> float:
    if trades.empty or int(summary.get("trades") or 0) < 100:
        return -1e9
    recent = _period_summary(trades, str(args.recent_start))
    monthly = _monthly_returns(trades)
    recent_mean = float(recent.get("mean_net_return") or 0.0)
    recent_win = float(recent.get("win_rate") or 0.0)
    pos_month = float(summary.get("positive_month_rate") or 0.0)
    mean = float(summary.get("mean_net_return") or 0.0)
    win = float(summary.get("win_rate") or 0.0)
    monthly_dd_penalty = abs(float(monthly.min() or 0.0)) if len(monthly) else 0.0
    trade_penalty = 0.0 if int(summary.get("trades") or 0) <= 12000 else (int(summary.get("trades") or 0) - 12000) / 12000.0
    return (
        mean * 100.0
        + recent_mean * 180.0
        + (win - 0.5) * 2.0
        + (recent_win - 0.5) * 3.0
        + (pos_month - 0.5) * 1.5
        - monthly_dd_penalty * 8.0
        - trade_penalty
    )


def _period_summary(trades: pd.DataFrame, start: str) -> dict[str, Any]:
    if trades.empty:
        return {"trades": 0, "win_rate": math.nan, "mean_net_return": math.nan, "sum_net_return": 0.0}
    start_ts = pd.Timestamp(start, tz="UTC")
    ts = pd.to_datetime(trades["entry_ts"], utc=True)
    sample = trades[ts >= start_ts]
    if sample.empty:
        return {"trades": 0, "win_rate": math.nan, "mean_net_return": math.nan, "sum_net_return": 0.0}
    ret = pd.to_numeric(sample["net_return"], errors="coerce").dropna()
    return {
        "trades": int(len(ret)),
        "win_rate": float((ret > 0).mean()) if len(ret) else math.nan,
        "mean_net_return": float(ret.mean()) if len(ret) else math.nan,
        "sum_net_return": float(ret.sum()) if len(ret) else 0.0,
    }


def _monthly_returns(trades: pd.DataFrame) -> pd.Series:
    if trades.empty:
        return pd.Series(dtype=float)
    df = trades.copy()
    df["month"] = pd.to_datetime(df["entry_ts"], utc=True).dt.strftime("%Y-%m")
    return df.groupby("month")["net_return"].sum()


def _prepare_feature_proxy_frame(args: argparse.Namespace) -> pd.DataFrame:
    path = FEATURE_DIR / args.dataset_id / "features.parquet"
    if not path.exists():
        raise FileNotFoundError(path)
    cols = [
        "close",
        "volume_usd",
        "listing_age_days",
        "train_eligible_90d",
        "ret_1",
        "ret_3",
        "ret_6",
        "range_pct",
        "close_to_high",
        "close_to_low",
        "atr_14_pct",
        "h4_ret_1",
        "h4_ret_6",
        "btc_regime_6",
    ]
    df = pd.read_parquet(path, columns=cols).sort_index()
    start = pd.Timestamp(args.start, tz="UTC")
    end = pd.Timestamp(args.end, tz="UTC") if args.end else pd.Timestamp.now(tz="UTC")
    ts = df.index.get_level_values("timestamp")
    df = df[(ts >= start) & (ts <= end)].copy()
    for col in cols:
        if col in df.columns and col != "btc_regime_6":
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df[
        (df["volume_usd"].fillna(0.0) >= float(args.min_volume_usd))
        & (df["listing_age_days"].fillna(0.0) >= float(args.min_listing_days))
        & (df["train_eligible_90d"].fillna(0.0) > 0)
        & df["close"].notna()
        & df["h4_ret_6"].notna()
    ].copy()
    latest_volume = df.groupby(level="symbol")["volume_usd"].last().sort_values(ascending=False)
    keep_symbols = set(latest_volume.head(int(args.max_symbols)).index.astype(str))
    df = df[df.index.get_level_values("symbol").astype(str).isin(keep_symbols)].copy()
    df["median_abs_1h_24"] = df["ret_1"].abs().groupby(level="symbol").transform(lambda s: s.rolling(24, min_periods=8).median())
    return df


def _research_feature_proxy(args: argparse.Namespace, prepared: pd.DataFrame | None = None) -> list[dict[str, Any]]:
    df = prepared.copy() if prepared is not None else _prepare_feature_proxy_frame(args)
    grouped_close = df["close"].groupby(level="symbol")
    horizon = max(1, int(args.max_hold_hours))
    future_close = grouped_close.shift(-horizon)
    df["fwd_ret"] = future_close / df["close"] - 1.0
    trend_min = abs(float(getattr(args, "h4_trend_min", 0.0) or 0.0))
    h4_allow = abs(float(getattr(args, "h4_countertrend_allow", 0.005) or 0.005))
    df["side"] = np.where(
        (df["h4_ret_6"] > trend_min) & (df["h4_ret_1"] > -h4_allow),
        "long",
        np.where((df["h4_ret_6"] < -trend_min) & (df["h4_ret_1"] < h4_allow), "short", ""),
    )
    if str(getattr(args, "side_mode", "both")) == "long":
        df.loc[df["side"] != "long", "side"] = ""
    elif str(getattr(args, "side_mode", "both")) == "short":
        df.loc[df["side"] != "short", "side"] = ""
    regimes = {item.strip() for item in str(getattr(args, "regime_allowlist", "") or "").split(",") if item.strip()}
    if regimes:
        df.loc[~df["btc_regime_6"].astype(str).isin(regimes), "side"] = ""
    if "median_abs_1h_24" in df.columns:
        median_abs_1h = df["median_abs_1h_24"]
    else:
        median_abs_1h = df["ret_1"].abs().groupby(level="symbol").transform(lambda s: s.rolling(24, min_periods=8).median())
    counter_limit = np.minimum(float(args.max_countertrend_move_pct), np.maximum(0.008, float(args.max_countertrend_multiple) * median_abs_1h))
    long_pullback = (df["side"] == "long") & (df["ret_3"] < 0) & (df["ret_3"].abs() <= counter_limit)
    short_pullback = (df["side"] == "short") & (df["ret_3"] > 0) & (df["ret_3"].abs() <= counter_limit)
    near = abs(float(getattr(args, "near_extreme_pct", 0.003) or 0.003))
    loose = abs(float(getattr(args, "loose_extreme_pct", 0.006) or 0.006))
    trigger_frac = abs(float(getattr(args, "trigger_range_frac", 0.25) or 0.25))
    long_trigger = (df["ret_1"] > 0) & (
        (df["close_to_high"] >= -near)
        | ((df["ret_1"] > df["range_pct"].abs() * trigger_frac) & (df["close_to_high"] >= -loose))
    )
    short_trigger = (df["ret_1"] < 0) & (
        (df["close_to_low"] <= near)
        | ((df["ret_1"].abs() > df["range_pct"].abs() * trigger_frac) & (df["close_to_low"] <= loose))
    )
    short_decay_gate = str(getattr(args, "short_decay_gate", "off") or "off")
    if short_decay_gate != "off":
        bounce = df["ret_3"].clip(lower=0.0)
        current_fade = df["ret_1"].abs()
        fade_confirmed = (current_fade >= bounce * float(args.short_decay_min_frac)) & (df["close_to_low"] <= loose)
        if short_decay_gate == "strict":
            fade_confirmed &= bounce <= float(args.short_max_bounce_pct)
        short_trigger &= fade_confirmed
    signal = (long_pullback & long_trigger) | (short_pullback & short_trigger)
    events = df[signal & df["fwd_ret"].notna()].copy()
    cost = 2.0 * (float(args.fee_bps_per_side) + float(args.slippage_bps_per_side)) / 10000.0
    rows: list[dict[str, Any]] = []
    last_exit_by_symbol: dict[str, pd.Timestamp] = {}
    for (entry_ts, symbol), row in events.iterrows():
        entry_ts = pd.Timestamp(entry_ts)
        symbol = str(symbol)
        if symbol in last_exit_by_symbol and entry_ts <= last_exit_by_symbol[symbol]:
            continue
        side = str(row["side"])
        gross = float(row["fwd_ret"])
        if side == "short":
            gross = -gross
        net = gross - cost
        adverse = -gross
        if gross >= float(args.target_pct):
            exit_reason = "target_proxy"
        elif adverse >= float(args.stop_pct):
            exit_reason = "stop_proxy"
        else:
            exit_reason = "horizon_proxy"
        exit_ts = entry_ts + pd.Timedelta(hours=horizon)
        rows.append(
            {
                "entry_ts": entry_ts.isoformat(),
                "exit_ts": exit_ts.isoformat(),
                "symbol": symbol,
                "side": side,
                "trigger": "feature_proxy_reversal",
                "entry": float(row["close"]),
                "exit": float(row["close"]) * (1.0 + float(row["fwd_ret"])),
                "exit_reason": exit_reason,
                "gross_return": gross,
                "net_return": net,
                "r_multiple": net / float(args.stop_pct),
                "counter_move": abs(float(row["ret_3"])),
                "counter_limit": float(counter_limit.loc[(entry_ts, symbol)]),
                "h4_trend_ret": float(row["h4_ret_6"]),
                "btc_regime_6": str(row.get("btc_regime_6") or ""),
            }
        )
        last_exit_by_symbol[symbol] = exit_ts
    return rows


def _select_symbols(args: argparse.Namespace) -> list[str]:
    path = FEATURE_DIR / args.dataset_id / "features.parquet"
    if not path.exists():
        paths = sorted(CACHE_DIR.glob("*_futures_1h.parquet"))
        return [_symbol_from_cache_path(path) for path in paths[: int(args.max_symbols)]]
    cols = ["close", "volume_usd", "listing_age_days", "train_eligible_90d"]
    df = pd.read_parquet(path, columns=cols)
    latest_ts = df.index.get_level_values("timestamp").max()
    latest = df.loc[df.index.get_level_values("timestamp") == latest_ts].reset_index()
    latest["volume_usd"] = pd.to_numeric(latest["volume_usd"], errors="coerce").fillna(0.0)
    latest["listing_age_days"] = pd.to_numeric(latest["listing_age_days"], errors="coerce").fillna(0.0)
    latest["train_eligible_90d"] = pd.to_numeric(latest["train_eligible_90d"], errors="coerce").fillna(0.0)
    latest = latest[
        (latest["symbol"] != "BTC/USDT")
        & (latest["volume_usd"] >= float(args.min_volume_usd))
        & (latest["listing_age_days"] >= float(args.min_listing_days))
        & (latest["train_eligible_90d"] > 0)
    ].sort_values("volume_usd", ascending=False)
    return latest["symbol"].astype(str).head(int(args.max_symbols)).tolist()


def _load_ohlcv(symbol: str, timeframe: str, args: argparse.Namespace) -> pd.DataFrame:
    safe = symbol.replace("/", "_").replace(":", "_")
    path = CACHE_DIR / f"{safe}_futures_{timeframe}.parquet"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_parquet(path)
    df = df.copy()
    df.index = pd.to_datetime(df.index, utc=True)
    df = df.sort_index()
    start = pd.Timestamp(args.start, tz="UTC")
    end = pd.Timestamp(args.end, tz="UTC") if args.end else pd.Timestamp.now(tz="UTC")
    df = df.loc[(df.index >= start) & (df.index <= end)]
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["open", "high", "low", "close"])


def _research_symbol(symbol: str, h1: pd.DataFrame, h4: pd.DataFrame, args: argparse.Namespace) -> list[dict[str, Any]]:
    h4_state = _h4_state(h4, args)
    h1 = h1.join(h4_state.reindex(h1.index, method="ffill"), how="left")
    ret = h1["close"].pct_change()
    median_abs_ret = ret.abs().rolling(24, min_periods=8).median()
    trades: list[dict[str, Any]] = []
    last_exit_ts: pd.Timestamp | None = None
    lookback = max(3, int(args.pullback_bars))
    for i in range(max(20, lookback + 3), len(h1) - 2):
        now = h1.index[i]
        if last_exit_ts is not None and now <= last_exit_ts:
            continue
        row = h1.iloc[i]
        side = str(row.get("trend_side") or "")
        if side not in {"long", "short"}:
            continue
        recent = h1.iloc[i - lookback + 1 : i + 1]
        counter_move = _countertrend_move(recent, side)
        counter_limit = min(float(args.max_countertrend_move_pct), max(0.008, float(args.max_countertrend_multiple) * float(median_abs_ret.iloc[i] or 0.0)))
        if counter_move <= 0 or counter_move > counter_limit:
            continue
        trigger = _reversal_trigger(h1.iloc[i - 4 : i + 1], side)
        if not trigger:
            continue
        trade = _simulate_trade(symbol, h1, i, side, trigger, counter_move, counter_limit, args)
        if trade:
            trades.append(trade)
            last_exit_ts = pd.Timestamp(trade["exit_ts"])
    return trades


def _h4_state(h4: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    out = pd.DataFrame(index=h4.index)
    close = h4["close"].astype(float)
    sma = close.rolling(int(args.trend_sma_bars), min_periods=max(4, int(args.trend_sma_bars) // 2)).mean()
    trend_ret = close / close.shift(int(args.trend_lookback_bars)) - 1.0
    out["h4_close"] = close
    out["h4_sma"] = sma
    out["h4_trend_ret"] = trend_ret
    out["trend_side"] = np.where((close > sma) & (trend_ret > 0), "long", np.where((close < sma) & (trend_ret < 0), "short", ""))
    return out


def _countertrend_move(recent: pd.DataFrame, side: str) -> float:
    first = float(recent["close"].iloc[0])
    last = float(recent["close"].iloc[-1])
    if first <= 0:
        return 0.0
    move = last / first - 1.0
    return -move if side == "long" else move


def _reversal_trigger(recent: pd.DataFrame, side: str) -> str:
    if len(recent) < 5:
        return ""
    prev = recent.iloc[-2]
    last = recent.iloc[-1]
    bullish_engulfing = last["close"] > last["open"] and prev["close"] < prev["open"] and last["close"] >= prev["open"]
    bearish_engulfing = last["close"] < last["open"] and prev["close"] > prev["open"] and last["close"] <= prev["open"]
    bottom_fractal = prev["low"] <= recent["low"].iloc[:-1].min() and last["close"] > prev["high"]
    top_fractal = prev["high"] >= recent["high"].iloc[:-1].max() and last["close"] < prev["low"]
    dip_reversing = last["close"] > prev["close"] and last["low"] >= prev["low"] and last["close"] > (last["open"] + last["low"]) / 2.0
    bounce_fading = last["close"] < prev["close"] and last["high"] <= prev["high"] and last["close"] < (last["open"] + last["high"]) / 2.0
    if side == "long":
        if bullish_engulfing:
            return "bullish_engulfing"
        if bottom_fractal:
            return "bottom_fractal"
        if dip_reversing:
            return "dip_reversing"
    else:
        if bearish_engulfing:
            return "bearish_engulfing"
        if top_fractal:
            return "top_fractal"
        if bounce_fading:
            return "bounce_fading"
    return ""


def _simulate_trade(
    symbol: str,
    h1: pd.DataFrame,
    entry_idx: int,
    side: str,
    trigger: str,
    counter_move: float,
    counter_limit: float,
    args: argparse.Namespace,
) -> dict[str, Any] | None:
    entry_ts = h1.index[entry_idx]
    entry = float(h1["close"].iloc[entry_idx])
    if entry <= 0:
        return None
    target = entry * (1 + float(args.target_pct) if side == "long" else 1 - float(args.target_pct))
    stop = entry * (1 - float(args.stop_pct) if side == "long" else 1 + float(args.stop_pct))
    exit_ts = h1.index[min(entry_idx + int(args.max_hold_hours), len(h1) - 1)]
    exit_price = float(h1["close"].loc[exit_ts])
    reason = "horizon"
    for j in range(entry_idx + 1, min(entry_idx + int(args.max_hold_hours), len(h1) - 1) + 1):
        bar = h1.iloc[j]
        ts = h1.index[j]
        high = float(bar["high"])
        low = float(bar["low"])
        if side == "long":
            stop_hit = low <= stop
            target_hit = high >= target
        else:
            stop_hit = high >= stop
            target_hit = low <= target
        if stop_hit or target_hit:
            exit_ts = ts
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
    net_return = gross - cost
    return {
        "entry_ts": entry_ts.isoformat(),
        "exit_ts": pd.Timestamp(exit_ts).isoformat(),
        "symbol": symbol,
        "side": side,
        "trigger": trigger,
        "entry": entry,
        "exit": exit_price,
        "exit_reason": reason,
        "gross_return": gross,
        "net_return": net_return,
        "r_multiple": net_return / float(args.stop_pct),
        "counter_move": counter_move,
        "counter_limit": counter_limit,
        "h4_trend_ret": float(h1["h4_trend_ret"].iloc[entry_idx]),
    }


def _summarize(trades: pd.DataFrame, args: argparse.Namespace) -> dict[str, Any]:
    if trades.empty:
        return {"trades": 0, "verdict": "NO_SIGNAL"}
    ret = pd.to_numeric(trades["net_return"], errors="coerce").dropna()
    wins = ret > 0
    by_side = {}
    for side, group in trades.groupby("side"):
        r = pd.to_numeric(group["net_return"], errors="coerce").dropna()
        by_side[str(side)] = {
            "trades": int(len(r)),
            "win_rate": float((r > 0).mean()) if len(r) else math.nan,
            "mean_net_return": float(r.mean()) if len(r) else math.nan,
            "median_net_return": float(r.median()) if len(r) else math.nan,
            "total_net_return_units": float(r.sum()) if len(r) else math.nan,
        }
    by_trigger = {}
    for trigger, group in trades.groupby("trigger"):
        r = pd.to_numeric(group["net_return"], errors="coerce").dropna()
        by_trigger[str(trigger)] = {
            "trades": int(len(r)),
            "win_rate": float((r > 0).mean()) if len(r) else math.nan,
            "mean_net_return": float(r.mean()) if len(r) else math.nan,
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
        "target_rate": float(trades["exit_reason"].astype(str).str.startswith("target").mean()),
        "stop_rate": float(trades["exit_reason"].astype(str).str.startswith("stop").mean()),
        "horizon_rate": float(trades["exit_reason"].astype(str).str.startswith("horizon").mean()),
        "by_side": by_side,
        "by_trigger": by_trigger,
        "verdict": verdict,
    }


def _monthly_positive_rate(trades: pd.DataFrame) -> float:
    if trades.empty:
        return math.nan
    df = trades.copy()
    df["month"] = pd.to_datetime(df["entry_ts"], utc=True).dt.strftime("%Y-%m")
    monthly = df.groupby("month")["net_return"].sum()
    return float((monthly > 0).mean()) if len(monthly) else math.nan


def _write_outputs(trades: pd.DataFrame, summary: dict[str, Any], args: argparse.Namespace) -> Path:
    out_id = args.out_id or f"trend_pullback_reversal_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
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
        "# Trend Pullback Reversal Research",
        "",
        f"Generated: {payload['generated_at']}",
        "",
        "## Summary",
        "",
    ]
    for key in ("trades", "symbols", "win_rate", "mean_net_return", "median_net_return", "total_net_return_units", "positive_month_rate", "target_rate", "stop_rate", "horizon_rate", "verdict"):
        lines.append(f"- {key}: {summary.get(key)}")
    lines.append("")
    lines.append("## By Side")
    for side, row in (summary.get("by_side") or {}).items():
        lines.append(f"- {side}: {row}")
    lines.append("")
    lines.append("## By Trigger")
    for trigger, row in (summary.get("by_trigger") or {}).items():
        lines.append(f"- {trigger}: {row}")
    lines.append("")
    return "\n".join(lines)


def _sweep_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Trend Pullback Reversal Sweep",
        "",
        f"Generated: {payload['generated_at']}",
        f"Configs tested: {payload.get('configs_tested')}",
        "",
        "## Best Config",
        "",
    ]
    lines.append(json.dumps(payload.get("best_config", {}), indent=2, sort_keys=True))
    lines.extend(["", "## Best Summary", ""])
    lines.append(json.dumps(payload.get("best_summary", {}), indent=2, sort_keys=True))
    lines.extend(["", "## Best Recent", ""])
    lines.append(json.dumps(payload.get("best_recent", {}), indent=2, sort_keys=True))
    lines.extend(["", "## Top 10", ""])
    for idx, row in enumerate(payload.get("top", []), start=1):
        lines.append(f"{idx}. score={row.get('score')}, side={row.get('side_mode')}, hold={row.get('max_hold_hours')}, mean={row.get('summary_mean_net_return')}, recent_mean={row.get('recent_mean_net_return')}, recent_win={row.get('recent_win_rate')}, trades={row.get('summary_trades')}")
    lines.append("")
    return "\n".join(lines)


def _symbol_from_cache_path(path: Path) -> str:
    name = path.name.replace("_futures_1h.parquet", "")
    return name.replace("_USDT", "/USDT")


if __name__ == "__main__":
    raise SystemExit(main())
