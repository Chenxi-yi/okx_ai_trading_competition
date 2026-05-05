#!/usr/bin/env python3
"""Select C-Auto features by BTC regime using sampled IC diagnostics."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
ENGINE_DIR = ROOT / "engine"
sys.path.insert(0, str(ENGINE_DIR))

from config.settings import BASE_DIR
from features import compute_ic_summary


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run regime-specific IC feature selection for C-Auto")
    p.add_argument("--dataset-id", default="c_auto_feature_store_v2")
    p.add_argument("--experiment-id", default="c_auto_regime_feature_selection_v1")
    p.add_argument("--label-col", default="fwd_ret_net_long_24")
    p.add_argument("--regime-col", default="btc_regime_6")
    p.add_argument("--max-timestamps-per-regime", type=int, default=2500)
    p.add_argument("--top-n", type=int, default=20)
    p.add_argument("--min-obs", type=int, default=1000)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    dataset_dir = BASE_DIR / "data" / "features" / args.dataset_id
    out_dir = BASE_DIR / "data" / "research" / "c_auto" / args.experiment_id
    out_dir.mkdir(parents=True, exist_ok=True)
    features = _read_frame(dataset_dir, "features")
    labels = _read_frame(dataset_dir, "labels")
    if args.regime_col not in features.columns:
        raise KeyError(f"{args.regime_col} not found in {dataset_dir}")
    if args.label_col not in labels.columns:
        raise KeyError(f"{args.label_col} not found in {dataset_dir}")

    all_rows: list[pd.DataFrame] = []
    candidate_sets: dict[str, Any] = {}
    for regime in sorted(features[args.regime_col].dropna().unique()):
        regime_mask = features[args.regime_col] == regime
        f = features.loc[regime_mask].drop(columns=["btc_regime_6", "btc_regime_3"], errors="ignore")
        l = labels.loc[labels.index.intersection(f.index)]
        f = f.loc[f.index.intersection(l.index)]
        f, l = _sample_by_timestamp(f, l, args.max_timestamps_per_regime)
        ic = compute_ic_summary(f, l, label_col=args.label_col, min_obs=args.min_obs)
        if ic.empty:
            continue
        ic.insert(0, "regime", regime)
        all_rows.append(ic)
        top = ic.head(args.top_n)
        positive = top[top["spearman_ic_mean"] > 0]["feature"].tolist()
        negative = top[top["spearman_ic_mean"] < 0]["feature"].tolist()
        candidate_sets[str(regime)] = {
            "rows": int(len(f)),
            "timestamps": int(f.index.get_level_values("timestamp").nunique()),
            "top_features": top["feature"].tolist(),
            "positive_features": positive,
            "negative_features": negative,
        }

    summary = pd.concat(all_rows, ignore_index=True) if all_rows else pd.DataFrame()
    _write_frame(summary, out_dir / "regime_ic_summary.parquet")
    summary.to_csv(out_dir / "regime_ic_summary.csv", index=False)
    (out_dir / "candidate_sets.json").write_text(json.dumps(candidate_sets, indent=2, sort_keys=True))
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "experiment_id": args.experiment_id,
        "dataset_id": args.dataset_id,
        "label_col": args.label_col,
        "regime_col": args.regime_col,
        "max_timestamps_per_regime": args.max_timestamps_per_regime,
        "top_n": args.top_n,
        "candidate_sets": candidate_sets,
        "artifacts": {
            "regime_ic_summary": "regime_ic_summary.parquet",
            "candidate_sets": "candidate_sets.json",
        },
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


def _read_frame(dataset_dir: Path, stem: str) -> pd.DataFrame:
    parquet = dataset_dir / f"{stem}.parquet"
    pkl = dataset_dir / f"{stem}.pkl"
    if parquet.exists():
        return pd.read_parquet(parquet)
    if pkl.exists():
        return pd.read_pickle(pkl)
    raise FileNotFoundError(f"Missing {stem}.parquet or {stem}.pkl in {dataset_dir}")


def _sample_by_timestamp(features: pd.DataFrame, labels: pd.DataFrame, max_timestamps: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    timestamps = pd.Index(features.index.get_level_values("timestamp").unique()).sort_values()
    if max_timestamps <= 0 or len(timestamps) <= max_timestamps:
        return features, labels
    positions = np.linspace(0, len(timestamps) - 1, max_timestamps).round().astype(int)
    sampled = set(timestamps[positions])
    f_mask = features.index.get_level_values("timestamp").isin(sampled)
    l_mask = labels.index.get_level_values("timestamp").isin(sampled)
    return features.loc[f_mask], labels.loc[l_mask]


def _write_frame(df: pd.DataFrame, path: Path) -> None:
    try:
        df.to_parquet(path)
    except Exception:
        df.to_pickle(path.with_suffix(".pkl"))


if __name__ == "__main__":
    raise SystemExit(main())
