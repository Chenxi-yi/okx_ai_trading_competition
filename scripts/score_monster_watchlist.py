#!/usr/bin/env python3
"""Score the latest cached 5m bars for monster-coin watchlist candidates."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from build_monster_dataset import (  # noqa: E402
    DEFAULT_HISTORY_MANIFEST,
    OUT_ROOT,
    _feature_columns,
    _load_symbol_data,
    _market_panels,
    _relpath,
    _sample_row,
)

DEFAULT_TRAINING = OUT_ROOT / "monster_samples_5m_v1" / "samples.parquet"
DEFAULT_FEATURE_SUMMARY = OUT_ROOT / "monster_samples_5m_v1" / "feature_summary.csv"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Score current monster-coin watchlist from cached 5m history")
    p.add_argument("--history-manifest", default=str(DEFAULT_HISTORY_MANIFEST))
    p.add_argument("--training-samples", default=str(DEFAULT_TRAINING))
    p.add_argument("--feature-summary", default=str(DEFAULT_FEATURE_SUMMARY))
    p.add_argument("--dataset-id", default=None)
    p.add_argument("--timeframe", default="5m")
    p.add_argument("--top-n", type=int, default=30)
    p.add_argument("--feature-count", type=int, default=25)
    p.add_argument("--market-snapshot", default=None, help="Optional CSV/Parquet from refresh_monster_latest.py")
    p.add_argument("--fresh-hours", type=float, default=0.25)
    p.add_argument("--min-quote-volume", type=float, default=1_000_000.0)
    p.add_argument("--max-spread-bps", type=float, default=50.0)
    p.add_argument("--min-depth-1pct-usd", type=float, default=5_000.0)
    p.add_argument("--min-score", type=float, default=0.75)
    p.add_argument("--max-ret-1h", type=float, default=0.25)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    manifest = json.loads(Path(args.history_manifest).read_text())
    data = _load_symbol_data(manifest["symbols"], args.timeframe)
    close_panel = pd.concat({sym: item.frame["close"] for sym, item in data.items()}, axis=1).sort_index()
    market = _market_panels(close_panel)
    training = pd.read_parquet(args.training_samples)
    feature_summary = pd.read_csv(args.feature_summary)
    score_features = _select_score_features(feature_summary, training, args.feature_count)
    market_snapshot = _load_market_snapshot(args.market_snapshot)

    created = pd.Timestamp.utcnow()
    rows = []
    for sym, item in data.items():
        latest_ts = item.frame.index[-1]
        row = _sample_row(sym, latest_ts, item, market, require_forward=False)
        if not row:
            continue
        row["bar_age_hours"] = float((created - latest_ts) / pd.Timedelta(hours=1))
        row["fresh_data_flag"] = int(row["bar_age_hours"] <= args.fresh_hours)
        if sym in market_snapshot:
            row.update(market_snapshot[sym])
        scored = _score_row(row, training, score_features)
        row.update(scored)
        row.update(_gates(row, args))
        rows.append(row)

    watchlist = pd.DataFrame(rows)
    if watchlist.empty:
        raise SystemExit("No watchlist rows built")
    watchlist = watchlist.sort_values("monster_score_adj", ascending=False)
    keep_cols = [
        "symbol",
        "sample_ts",
        "monster_score",
        "monster_score_adj",
        "bar_age_hours",
        "fresh_data_flag",
        "liquidity_gate",
        "trade_candidate_flag",
        "market_event_flag",
        "trigger_reasons",
        "rvol_6h",
        "rvol_24h",
        "range_pct_6h",
        "range_pct_24h",
        "volume_mean_15m",
        "volume_1h_vs_24h",
        "ret_1h",
        "ret_6h",
        "ret_24h",
        "cs_rank_ret_6h",
        "cs_rank_ret_24h",
        "market_ret_24h",
        "breadth_up_20_24h",
        "quote_volume_24h",
        "spread_bps",
        "depth_1pct_usd",
    ]
    keep_cols = [c for c in keep_cols if c in watchlist.columns]

    dataset_id = args.dataset_id or f"monster_watchlist_5m_{created.strftime('%Y%m%d_%H%M%S')}"
    out_dir = OUT_ROOT / dataset_id
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "watchlist.csv"
    parquet_path = out_dir / "watchlist.parquet"
    json_path = out_dir / "watchlist_top.json"
    watchlist.to_parquet(parquet_path)
    watchlist[keep_cols].to_csv(csv_path, index=False)
    json_path.write_text(json.dumps(watchlist[keep_cols].head(args.top_n).to_dict(orient="records"), indent=2, default=str))

    payload = {
        "dataset_id": dataset_id,
        "created_at": created.isoformat(),
        "history_manifest": _relpath(Path(args.history_manifest)),
        "training_samples": _relpath(Path(args.training_samples)),
        "feature_summary": _relpath(Path(args.feature_summary)),
        "market_snapshot": _relpath(Path(args.market_snapshot)) if args.market_snapshot else None,
        "gates": {
            "fresh_hours": args.fresh_hours,
            "min_quote_volume": args.min_quote_volume,
            "max_spread_bps": args.max_spread_bps,
            "min_depth_1pct_usd": args.min_depth_1pct_usd,
            "min_score": args.min_score,
            "max_ret_1h": args.max_ret_1h,
        },
        "symbols_scored": int(len(watchlist)),
        "score_features": score_features,
        "artifacts": {
            "watchlist_csv": _relpath(csv_path),
            "watchlist_parquet": _relpath(parquet_path),
            "watchlist_top_json": _relpath(json_path),
        },
        "top": watchlist[keep_cols].head(args.top_n).to_dict(orient="records"),
    }
    (out_dir / "manifest.json").write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    print("\nTop watchlist:")
    print(watchlist[keep_cols].head(args.top_n).to_string(index=False))
    return 0


def _select_score_features(feature_summary: pd.DataFrame, training: pd.DataFrame, n: int) -> list[dict[str, Any]]:
    excluded_prefixes = ("volume_sum_",)
    rows = []
    usable = set(_feature_columns(training))
    for row in feature_summary.to_dict(orient="records"):
        feature = row["feature"]
        if feature not in usable or feature.startswith(excluded_prefixes):
            continue
        if pd.isna(row.get("auc")) or pd.isna(row.get("auc_distance")):
            continue
        if row.get("positive_nan_rate", 1.0) > 0.10 or row.get("negative_nan_rate", 1.0) > 0.10:
            continue
        rows.append(
            {
                "feature": feature,
                "auc": float(row["auc"]),
                "weight": float(row["auc_distance"]),
                "direction": 1 if float(row["auc"]) >= 0.5 else -1,
            }
        )
        if len(rows) >= n:
            break
    return rows


def _score_row(row: dict[str, Any], training: pd.DataFrame, score_features: list[dict[str, Any]]) -> dict[str, Any]:
    total_weight = 0.0
    score = 0.0
    reasons = []
    for spec in score_features:
        feature = spec["feature"]
        value = row.get(feature)
        if value is None or pd.isna(value):
            continue
        pct = _percentile(training[feature], float(value))
        component = pct if spec["direction"] > 0 else 1.0 - pct
        weight = spec["weight"]
        score += component * weight
        total_weight += weight
        if component >= 0.85:
            label = "high" if spec["direction"] > 0 else "low"
            reasons.append(f"{feature}:{label}:{component:.2f}")
    raw = score / total_weight if total_weight else 0.0
    adjusted = raw * (0.75 if row.get("market_event_flag") else 1.0)
    return {
        "monster_score": raw,
        "monster_score_adj": adjusted,
        "trigger_reasons": "; ".join(reasons[:8]),
    }


def _gates(row: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    quote_volume = row.get("quote_volume_24h")
    spread = row.get("spread_bps")
    depth = row.get("depth_1pct_usd")
    liquidity_gate = int(
        quote_volume is not None
        and not pd.isna(quote_volume)
        and quote_volume >= args.min_quote_volume
        and spread is not None
        and not pd.isna(spread)
        and spread <= args.max_spread_bps
        and depth is not None
        and not pd.isna(depth)
        and depth >= args.min_depth_1pct_usd
    )
    trade_candidate = int(
        row.get("fresh_data_flag") == 1
        and liquidity_gate == 1
        and row.get("market_event_flag") == 0
        and row.get("monster_score_adj", 0.0) >= args.min_score
        and (row.get("ret_1h") is None or row.get("ret_1h") <= args.max_ret_1h)
    )
    return {"liquidity_gate": liquidity_gate, "trade_candidate_flag": trade_candidate}


def _load_market_snapshot(path: str | None) -> dict[str, dict[str, Any]]:
    if not path:
        return {}
    p = Path(path)
    df = pd.read_parquet(p) if p.suffix == ".parquet" else pd.read_csv(p)
    if df.empty or "symbol" not in df:
        return {}
    keep = ["quote_volume_24h", "spread_bps", "depth_1pct_usd", "last", "bid", "ask"]
    out: dict[str, dict[str, Any]] = {}
    for row in df.to_dict(orient="records"):
        out[row["symbol"]] = {k: row.get(k) for k in keep if k in row}
    return out


def _percentile(series: pd.Series, value: float) -> float:
    s = pd.to_numeric(series, errors="coerce").dropna()
    if s.empty:
        return 0.5
    return float((s <= value).mean())


if __name__ == "__main__":
    raise SystemExit(main())
