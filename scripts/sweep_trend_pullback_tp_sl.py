#!/usr/bin/env python3
"""Sweep TP/SL for selected trend-pullback reversal variants using 1h paths."""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT / "engine" / "data" / "cache"
OUT_ROOT = ROOT / "engine" / "data" / "research" / "trend_pullback_reversal"
CORE_SCRIPT = ROOT / "scripts" / "run_trend_pullback_reversal_variants.py"


def load_core() -> Any:
    spec = importlib.util.spec_from_file_location("trend_pullback_core", CORE_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {CORE_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[str(spec.name)] = module
    spec.loader.exec_module(module)
    return module


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sweep TP/SL with 1h path simulation")
    p.add_argument("--source-dir", default="engine/data/research/trend_pullback_reversal/trend_pullback_variants_4way_20260517")
    p.add_argument("--out-id", default="")
    p.add_argument("--targets", default="0.015,0.02,0.025,0.03,0.04,0.05")
    p.add_argument("--stops", default="0.008,0.01,0.012,0.015,0.02,0.025")
    p.add_argument("--max-hold-hours", type=int, default=12)
    p.add_argument("--same-bar-policy", choices=["stop_first", "target_first"], default="stop_first")
    p.add_argument("--include-cluster-quality60", action="store_true")
    p.add_argument("--cluster-trades", default="engine/data/research/trend_pullback_reversal/trend_pullback_deep_dive_selected3_20260517/cluster_elite60_mean100bp_trades.csv")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    core = load_core()
    source_dir = ROOT / args.source_dir
    candidate_path = source_dir / "candidate_events.csv"
    summary_path = source_dir / "summary.json"
    if not candidate_path.exists():
        raise FileNotFoundError(candidate_path)
    candidates = pd.read_csv(candidate_path, parse_dates=["entry_ts", "exit_ts"])
    source_summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
    base_args = argparse.Namespace(**(source_summary.get("args") or {}))
    base_args.max_hold_hours = int(args.max_hold_hours)

    selected = {
        "quality_top20": core.apply_nonoverlap(core.select_quality_top_frac(candidates, 0.20)),
        "rank_top1": core.apply_nonoverlap(core.select_rank_top_n(candidates, 1)),
    }
    if args.include_cluster_quality60:
        cluster_path = ROOT / args.cluster_trades
        if not cluster_path.exists():
            raise FileNotFoundError(cluster_path)
        cluster = pd.read_csv(cluster_path, parse_dates=["entry_ts", "exit_ts"])
        cluster = cluster[pd.to_numeric(cluster["quality_score"], errors="coerce") >= 0.60].copy()
        selected["cluster_elite_quality60"] = core.apply_nonoverlap(cluster)
    targets = [float(x) for x in args.targets.split(",") if x.strip()]
    stops = [float(x) for x in args.stops.split(",") if x.strip()]
    out_id = args.out_id or f"trend_pullback_tp_sl_sweep_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    out_dir = OUT_ROOT / out_id
    out_dir.mkdir(parents=True, exist_ok=True)

    price_cache: dict[str, pd.DataFrame] = {}
    rows: list[dict[str, Any]] = []
    for variant, entries in selected.items():
        entries.to_csv(out_dir / f"{variant}_entries.csv", index=False)
        for target in targets:
            for stop in stops:
                trades = simulate_entries(entries, target, stop, int(args.max_hold_hours), args.same_bar_policy, price_cache)
                name = f"{variant}_tp{int(target * 10000):04d}_sl{int(stop * 10000):04d}"
                trades.to_csv(out_dir / f"{name}_trades.csv", index=False)
                summary = summarize_path(trades)
                rows.append(
                    {
                        "variant": variant,
                        "target_pct": target,
                        "stop_pct": stop,
                        "rr": target / stop if stop else math.nan,
                        "same_bar_policy": args.same_bar_policy,
                        **summary,
                    }
                )

    comparison = pd.DataFrame(rows).sort_values(["score", "mean_net_return"], ascending=False)
    comparison.to_csv(out_dir / "tp_sl_sweep.csv", index=False)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_dir": str(source_dir.relative_to(ROOT)),
        "same_bar_policy": args.same_bar_policy,
        "max_hold_hours": int(args.max_hold_hours),
        "comparison": comparison.to_dict(orient="records"),
        "artifacts": {"tp_sl_sweep": "tp_sl_sweep.csv"},
    }
    (out_dir / "summary.json").write_text(json.dumps(payload, indent=2, sort_keys=True))
    (out_dir / "report.md").write_text(markdown_report(payload, comparison))
    print(json.dumps({"out_dir": str(out_dir.relative_to(ROOT)), **payload}, indent=2, sort_keys=True))
    return 0


def simulate_entries(
    entries: pd.DataFrame,
    target_pct: float,
    stop_pct: float,
    max_hold_hours: int,
    same_bar_policy: str,
    price_cache: dict[str, pd.DataFrame],
) -> pd.DataFrame:
    rows = []
    for row in entries.sort_values("entry_ts").to_dict(orient="records"):
        symbol = str(row["symbol"])
        path = price_cache.get(symbol)
        if path is None:
            path = load_ohlcv(symbol)
            price_cache[symbol] = path
        if path.empty:
            continue
        entry_ts = pd.Timestamp(row["entry_ts"])
        if entry_ts.tzinfo is None:
            entry_ts = entry_ts.tz_localize("UTC")
        entry = float(row["entry"])
        side = str(row["side"])
        if entry <= 0 or side not in {"long", "short"}:
            continue
        future = path[path.index > entry_ts].head(max_hold_hours)
        if future.empty:
            continue
        target = entry * (1.0 + target_pct) if side == "long" else entry * (1.0 - target_pct)
        stop = entry * (1.0 - stop_pct) if side == "long" else entry * (1.0 + stop_pct)
        exit_ts = future.index[-1]
        exit_price = float(future["close"].iloc[-1])
        reason = "horizon"
        for ts, bar in future.iterrows():
            high = float(bar["high"])
            low = float(bar["low"])
            if side == "long":
                stop_hit = low <= stop
                target_hit = high >= target
            else:
                stop_hit = high >= stop
                target_hit = low <= target
            if stop_hit and target_hit:
                exit_ts = ts
                if same_bar_policy == "target_first":
                    exit_price = target
                    reason = "target_same_bar"
                else:
                    exit_price = stop
                    reason = "stop_same_bar"
                break
            if stop_hit:
                exit_ts = ts
                exit_price = stop
                reason = "stop"
                break
            if target_hit:
                exit_ts = ts
                exit_price = target
                reason = "target"
                break
        gross = exit_price / entry - 1.0
        if side == "short":
            gross = -gross
        cost = 2.0 * (5.0 + 2.0) / 10000.0
        out = dict(row)
        out.update(
            {
                "exit_ts": pd.Timestamp(exit_ts).isoformat(),
                "exit": exit_price,
                "target_pct": target_pct,
                "stop_pct": stop_pct,
                "exit_reason": reason,
                "gross_return": gross,
                "net_return": gross - cost,
                "r_multiple": (gross - cost) / stop_pct if stop_pct else math.nan,
            }
        )
        rows.append(out)
    return pd.DataFrame(rows)


def load_ohlcv(symbol: str) -> pd.DataFrame:
    safe = symbol.replace("/", "_").replace(":", "_")
    path = CACHE_DIR / f"{safe}_futures_1h.parquet"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_parquet(path).copy()
    df.index = pd.to_datetime(df.index, utc=True)
    df = df.sort_index()
    for col in ("open", "high", "low", "close"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["open", "high", "low", "close"])


def summarize_path(trades: pd.DataFrame) -> dict[str, Any]:
    if trades.empty:
        return {
            "trades": 0,
            "win_rate": math.nan,
            "mean_net_return": math.nan,
            "median_net_return": math.nan,
            "total_net_return_units": 0.0,
            "positive_month_rate": math.nan,
            "worst_month_units": math.nan,
            "score": -1e9,
        }
    ret = pd.to_numeric(trades["net_return"], errors="coerce").dropna()
    tmp = trades.copy()
    tmp["month"] = pd.to_datetime(tmp["entry_ts"], utc=True).dt.strftime("%Y-%m")
    monthly = tmp.groupby("month")["net_return"].sum()
    win = float((ret > 0).mean())
    mean = float(ret.mean())
    pos_month = float((monthly > 0).mean()) if len(monthly) else math.nan
    worst_month = float(monthly.min()) if len(monthly) else math.nan
    score = mean * 120.0 + (win - 0.5) * 2.0 + (pos_month - 0.5) * 1.8 - abs(min(0.0, worst_month)) * 5.0
    return {
        "trades": int(len(ret)),
        "win_rate": win,
        "mean_net_return": mean,
        "median_net_return": float(ret.median()),
        "total_net_return_units": float(ret.sum()),
        "positive_month_rate": pos_month,
        "worst_month_units": worst_month,
        "target_rate": float(trades["exit_reason"].astype(str).str.startswith("target").mean()),
        "stop_rate": float(trades["exit_reason"].astype(str).str.startswith("stop").mean()),
        "horizon_rate": float((trades["exit_reason"].astype(str) == "horizon").mean()),
        "score": float(score),
    }


def markdown_report(payload: dict[str, Any], comparison: pd.DataFrame) -> str:
    lines = [
        "# Trend Pullback TP/SL Sweep",
        "",
        f"Generated: {payload['generated_at']}",
        f"Source: `{payload['source_dir']}`",
        f"Same-bar policy: `{payload['same_bar_policy']}`",
        "",
        "| Variant | TP | SL | Trades | Win | Mean | Total Units | Pos Months | Worst Month | Target | Stop |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in comparison.head(30).to_dict(orient="records"):
        lines.append(
            "| {variant} | {target_pct:.2%} | {stop_pct:.2%} | {trades} | {win_rate:.2%} | {mean_net_return:.3%} | {total_net_return_units:.2f} | {positive_month_rate:.2%} | {worst_month_units:.2f} | {target_rate:.2%} | {stop_rate:.2%} |".format(
                **row
            )
        )
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
