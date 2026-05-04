#!/usr/bin/env python3
"""Materialize feature, label, validation, and IC datasets."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List

import pandas as pd

ENGINE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE_DIR))

from config.settings import BASE_DIR
from data.fetcher import fetch_universe
from features import (
    build_feature_panel,
    build_label_panel,
    build_default_feature_registry,
    build_default_label_registry,
    build_microstructure_feature_panel,
    build_microstructure_feature_registry,
    compute_ic_summary,
    label_registry_to_dict,
    registry_to_dict,
    validate_feature_label_panel,
)
from research.manifest import build_dataset_manifest
from research.walk_forward import build_purged_walk_forward_folds, folds_to_dicts
from data.catalog import DataCatalog


OUT_DIR = BASE_DIR / "data" / "features"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build point-in-time feature research dataset")
    p.add_argument("--symbols", default="BTC/USDT,ETH/USDT,SOL/USDT", help="Comma-separated ccxt symbols")
    p.add_argument("--start", default="2024-01-01")
    p.add_argument("--end", default="2026-03-31")
    p.add_argument("--timeframe", default="1h")
    p.add_argument("--mode", default="futures")
    p.add_argument("--dataset-id", default=None)
    p.add_argument("--label-col", default="fwd_ret_6")
    p.add_argument("--microstructure-dataset", default=None, help="Optional path to a dataset from scripts/fetch_microstructure.py")
    p.add_argument("--microstructure-trade-freq", default="1min", help="Aggregation bucket for trade-flow features")
    p.add_argument("--fee-bps", type=float, default=5.0, help="One-way fee assumption in basis points")
    p.add_argument("--slippage-bps", type=float, default=2.0, help="One-way slippage assumption in basis points")
    p.add_argument("--funding-cost-bps-per-bar", type=float, default=0.0, help="Per-bar funding cost haircut for cost-adjusted labels")
    p.add_argument("--wf-train-bars", type=int, default=24 * 14, help="Walk-forward train window in bars")
    p.add_argument("--wf-test-bars", type=int, default=24 * 3, help="Walk-forward test window in bars")
    p.add_argument("--wf-purge-bars", type=int, default=24, help="Purged gap before each test window")
    p.add_argument("--no-cache", action="store_true", help="Skip local OHLCV cache")
    p.add_argument("--register-catalog", action="store_true", help="Register output dataset in DataCatalog")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    symbols: List[str] = [s.strip() for s in args.symbols.split(",") if s.strip()]
    dataset_id = args.dataset_id or f"{args.mode}_{args.timeframe}_{args.start}_{args.end}".replace("/", "_")
    out = OUT_DIR / dataset_id
    out.mkdir(parents=True, exist_ok=True)

    price_data = fetch_universe(
        symbols,
        start=args.start,
        end=args.end,
        mode=args.mode,
        timeframe=args.timeframe,
        use_cache=not args.no_cache,
    )
    features = build_feature_panel(price_data)
    micro_features = pd.DataFrame()
    if args.microstructure_dataset:
        micro_features = build_microstructure_feature_panel(
            Path(args.microstructure_dataset),
            trade_freq=args.microstructure_trade_freq,
        )
        if not micro_features.empty:
            micro_features = _align_micro_features(micro_features, features.index)
            features = pd.concat([features, micro_features], axis=1).sort_index()
    labels = build_label_panel(
        price_data,
        fee_bps=args.fee_bps,
        slippage_bps=args.slippage_bps,
        funding_cost_bps_per_bar=args.funding_cost_bps_per_bar,
    )
    feature_registry = build_default_feature_registry(frequency=args.timeframe)
    if not micro_features.empty:
        feature_registry.update(build_microstructure_feature_registry(frequency=args.microstructure_trade_freq))
    label_registry = build_default_label_registry()
    feature_registry_dict = registry_to_dict(feature_registry)
    label_registry_dict = label_registry_to_dict(label_registry)
    report = validate_feature_label_panel(
        features,
        labels,
        feature_registry=feature_registry,
        expected_frequency=args.timeframe,
    )
    ic = compute_ic_summary(features, labels, label_col=args.label_col)
    folds = build_purged_walk_forward_folds(
        features.index.intersection(labels.index),
        train_bars=args.wf_train_bars,
        test_bars=args.wf_test_bars,
        purge_bars=args.wf_purge_bars,
    )

    _write_frame(features, out / "features.parquet")
    _write_frame(labels, out / "labels.parquet")
    _write_frame(ic, out / "ic_summary.parquet")
    (out / "validation.json").write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    (out / "feature_registry.json").write_text(json.dumps(feature_registry_dict, indent=2, sort_keys=True))
    (out / "label_registry.json").write_text(json.dumps(label_registry_dict, indent=2, sort_keys=True))
    (out / "walk_forward_folds.json").write_text(json.dumps(folds_to_dicts(folds), indent=2, sort_keys=True))
    metadata = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dataset_id": dataset_id,
        "symbols": symbols,
        "loaded_symbols": sorted(price_data),
        "start": args.start,
        "end": args.end,
        "timeframe": args.timeframe,
        "mode": args.mode,
        "label_col": args.label_col,
        "microstructure_dataset": args.microstructure_dataset,
        "microstructure_features": int(micro_features.shape[1]) if not micro_features.empty else 0,
        "cost_assumptions": {
            "fee_bps": args.fee_bps,
            "slippage_bps": args.slippage_bps,
            "funding_cost_bps_per_bar": args.funding_cost_bps_per_bar,
        },
        "walk_forward": {
            "folds": len(folds),
            "train_bars": args.wf_train_bars,
            "test_bars": args.wf_test_bars,
            "purge_bars": args.wf_purge_bars,
        },
        "rows": int(len(features.index.intersection(labels.index))),
        "validation_status": report.status,
    }
    (out / "metadata.json").write_text(json.dumps(metadata, indent=2, sort_keys=True))
    manifest = build_dataset_manifest(
        dataset_id=dataset_id,
        out_dir=out,
        symbols=symbols,
        loaded_symbols=price_data.keys(),
        start=args.start,
        end=args.end,
        timeframe=args.timeframe,
        mode=args.mode,
        label_col=args.label_col,
        features=features,
        labels=labels,
        validation_report=report.to_dict(),
        feature_registry=feature_registry_dict,
        label_registry=label_registry_dict,
        cost_assumptions=metadata["cost_assumptions"],
    )
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    if args.register_catalog:
        DataCatalog().register_feature_dataset(dataset_id, out)
        metadata["catalog_registered"] = True
        (out / "metadata.json").write_text(json.dumps(metadata, indent=2, sort_keys=True))

    print(json.dumps(metadata, indent=2, sort_keys=True))
    if not ic.empty:
        print("\nTop IC features:")
        print(ic.head(15).to_string(index=False))
    return 0 if report.status.startswith(("ok", "warn")) else 1


def _write_frame(df: pd.DataFrame, path: Path) -> None:
    try:
        df.to_parquet(path)
    except Exception:
        df.to_pickle(path.with_suffix(".pkl"))


def _align_micro_features(micro_features: pd.DataFrame, target_index: pd.Index) -> pd.DataFrame:
    if micro_features.empty or len(target_index) == 0 or not isinstance(target_index, pd.MultiIndex):
        return micro_features
    frames = []
    target_symbols = target_index.get_level_values("symbol").unique()
    for symbol in target_symbols:
        target_ts = pd.DatetimeIndex(target_index[target_index.get_level_values("symbol") == symbol].get_level_values("timestamp"))
        if symbol not in micro_features.index.get_level_values("symbol"):
            aligned = pd.DataFrame(index=target_ts, columns=micro_features.columns, dtype=float)
        else:
            src = micro_features.xs(symbol, level="symbol").sort_index()
            combined_index = src.index.union(target_ts).sort_values()
            aligned = src.reindex(combined_index).ffill().reindex(target_ts)
        aligned["symbol"] = symbol
        aligned = aligned.set_index("symbol", append=True)
        aligned.index.names = ["timestamp", "symbol"]
        frames.append(aligned)
    return pd.concat(frames).sort_index() if frames else pd.DataFrame()


if __name__ == "__main__":
    raise SystemExit(main())
