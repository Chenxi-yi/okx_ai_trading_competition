#!/usr/bin/env python3
"""Materialize the first C-Auto point-in-time feature store."""

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

from config.settings import BASE_DIR, DATA_DIR
from data.catalog import DataCatalog
from features import (
    build_default_feature_registry,
    build_default_label_registry,
    build_feature_panel,
    build_label_panel,
    compute_ic_summary,
    label_registry_to_dict,
    registry_to_dict,
    validate_feature_label_panel,
)
from features.registry import FeatureSpec
from research.manifest import build_dataset_manifest
from research.walk_forward import build_purged_walk_forward_folds, folds_to_dicts


DEFAULT_QUALITY_ID = "c_auto_dataset_quality_v1"
DEFAULT_OHLCV_RUN = "c_auto_universe_vol5m_5m_15m_20240101_20260505"
DEFAULT_DERIV_RUN = "c_auto_deriv_vol5m_5m_20240101_20260505"
DEFAULT_SNAPSHOT_RUN = "c_auto_market_quality_snapshot_20260505"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build C-Auto v1 feature dataset")
    p.add_argument("--dataset-id", default="c_auto_feature_store_v1")
    p.add_argument("--quality-id", default=DEFAULT_QUALITY_ID)
    p.add_argument("--ohlcv-run-id", default=DEFAULT_OHLCV_RUN)
    p.add_argument("--deriv-run-id", default=DEFAULT_DERIV_RUN)
    p.add_argument("--snapshot-run-id", default=DEFAULT_SNAPSHOT_RUN)
    p.add_argument("--start", default="2024-01-01")
    p.add_argument("--end", default="2026-05-05")
    p.add_argument("--min-train-1h-rows", type=int, default=2160)
    p.add_argument("--label-col", default="fwd_ret_net_long_24")
    p.add_argument("--fee-bps", type=float, default=5.0)
    p.add_argument("--slippage-bps", type=float, default=2.0)
    p.add_argument("--funding-cost-bps-per-bar", type=float, default=0.0)
    p.add_argument("--wf-train-bars", type=int, default=24 * 90)
    p.add_argument("--wf-test-bars", type=int, default=24 * 14)
    p.add_argument("--wf-purge-bars", type=int, default=24)
    p.add_argument("--ic-max-timestamps", type=int, default=3000, help="Sampled timestamps for first-pass IC diagnostics")
    p.add_argument("--register-catalog", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    out_dir = BASE_DIR / "data" / "features" / args.dataset_id
    out_dir.mkdir(parents=True, exist_ok=True)

    quality_dir = BASE_DIR / "data" / "quality" / args.quality_id
    quality = _read_quality(quality_dir)
    quality = quality[quality["has_core_inputs"].astype(bool)].copy()
    if quality.empty:
        raise RuntimeError(f"No core-ready symbols in {quality_dir}")
    symbols = quality["symbol"].astype(str).tolist()

    price_data: dict[str, pd.DataFrame] = {}
    extras: list[pd.DataFrame] = []
    for symbol in symbols:
        one_h = _load_ohlcv(symbol, "1h", args.start, args.end)
        if one_h.empty:
            continue
        one_h = _attach_funding(one_h, symbol, args.deriv_run_id)
        price_data[symbol] = one_h
        extra = _extra_features_for_symbol(one_h, symbol, args.deriv_run_id, args.snapshot_run_id)
        if not extra.empty:
            extras.append(extra)

    if not price_data:
        raise RuntimeError("No 1h price data loaded")

    base_features = build_feature_panel(price_data)
    extra_features = pd.concat(extras).sort_index() if extras else pd.DataFrame(index=base_features.index)
    extra_features = extra_features.reindex(base_features.index)
    features = pd.concat([base_features, extra_features], axis=1).sort_index()
    features = _attach_quality_flags(features, quality, min_train_1h_rows=args.min_train_1h_rows)
    labels = build_label_panel(
        price_data,
        horizons=(1, 3, 6, 12, 24),
        fee_bps=args.fee_bps,
        slippage_bps=args.slippage_bps,
        funding_cost_bps_per_bar=args.funding_cost_bps_per_bar,
    )

    feature_registry = build_default_feature_registry(frequency="1h")
    feature_registry.update(_extra_feature_registry())
    feature_registry_dict = registry_to_dict(feature_registry)
    label_registry = build_default_label_registry()
    label_registry_dict = label_registry_to_dict(label_registry)
    report = validate_feature_label_panel(
        features,
        labels,
        feature_registry=feature_registry,
        expected_frequency="1h",
        max_feature_nan_pct=0.55,
    )
    ic = _compute_sampled_ic(features, labels, label_col=args.label_col, max_timestamps=args.ic_max_timestamps)
    folds = build_purged_walk_forward_folds(
        features.index.intersection(labels.index),
        train_bars=args.wf_train_bars,
        test_bars=args.wf_test_bars,
        purge_bars=args.wf_purge_bars,
    )

    _write_frame(features, out_dir / "features.parquet")
    _write_frame(labels, out_dir / "labels.parquet")
    _write_frame(ic, out_dir / "ic_summary.parquet")
    (out_dir / "validation.json").write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    (out_dir / "feature_registry.json").write_text(json.dumps(feature_registry_dict, indent=2, sort_keys=True))
    (out_dir / "label_registry.json").write_text(json.dumps(label_registry_dict, indent=2, sort_keys=True))
    (out_dir / "walk_forward_folds.json").write_text(json.dumps(folds_to_dicts(folds), indent=2, sort_keys=True))

    metadata = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dataset_id": args.dataset_id,
        "quality_id": args.quality_id,
        "symbols": symbols,
        "loaded_symbols": sorted(price_data),
        "start": args.start,
        "end": args.end,
        "timeframe": "1h",
        "mode": "futures",
        "label_col": args.label_col,
        "rows": int(len(features.index.intersection(labels.index))),
        "features": int(features.shape[1]),
        "labels": int(labels.shape[1]),
        "train_eligible_90d_symbols": int(quality["train_eligible_90d"].sum()),
        "validation_status": report.status,
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
        "ic_diagnostics": {
            "max_timestamps": args.ic_max_timestamps,
            "rows": int(ic["n_obs"].max()) if not ic.empty and "n_obs" in ic else 0,
        },
    }
    (out_dir / "metadata.json").write_text(json.dumps(metadata, indent=2, sort_keys=True))
    manifest = build_dataset_manifest(
        dataset_id=args.dataset_id,
        out_dir=out_dir,
        symbols=symbols,
        loaded_symbols=price_data.keys(),
        start=args.start,
        end=args.end,
        timeframe="1h",
        mode="futures",
        label_col=args.label_col,
        features=features,
        labels=labels,
        validation_report=report.to_dict(),
        feature_registry=feature_registry_dict,
        label_registry=label_registry_dict,
        cost_assumptions=metadata["cost_assumptions"],
    )
    manifest["quality_id"] = args.quality_id
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))

    if args.register_catalog:
        DataCatalog().register_feature_dataset(args.dataset_id, out_dir)

    print(json.dumps(metadata, indent=2, sort_keys=True))
    if not ic.empty:
        print("\nTop IC features:")
        print(ic.head(20).to_string(index=False))
    return 0 if report.status.startswith(("ok", "warn")) else 1


def _read_quality(quality_dir: Path) -> pd.DataFrame:
    parquet = quality_dir / "symbol_quality.parquet"
    csv = quality_dir / "symbol_quality.csv"
    if parquet.exists():
        return pd.read_parquet(parquet)
    if csv.exists():
        return pd.read_csv(csv)
    raise FileNotFoundError(f"Missing quality dataset in {quality_dir}")


def _compute_sampled_ic(features: pd.DataFrame, labels: pd.DataFrame, label_col: str, max_timestamps: int) -> pd.DataFrame:
    if max_timestamps <= 0 or features.empty or labels.empty or not isinstance(features.index, pd.MultiIndex):
        return compute_ic_summary(features, labels, label_col=label_col)
    timestamps = pd.Index(features.index.get_level_values("timestamp").unique()).sort_values()
    if len(timestamps) <= max_timestamps:
        return compute_ic_summary(features, labels, label_col=label_col)
    positions = np.linspace(0, len(timestamps) - 1, max_timestamps).round().astype(int)
    sampled = set(timestamps[positions])
    feature_mask = features.index.get_level_values("timestamp").isin(sampled)
    label_mask = labels.index.get_level_values("timestamp").isin(sampled)
    return compute_ic_summary(features.loc[feature_mask], labels.loc[label_mask], label_col=label_col)


def _load_ohlcv(symbol: str, timeframe: str, start: str, end: str) -> pd.DataFrame:
    safe = _safe_symbol(symbol)
    for ext in ("parquet", "pkl"):
        path = DATA_DIR / f"{safe}_futures_{timeframe}.{ext}"
        if not path.exists():
            continue
        df = pd.read_parquet(path) if ext == "parquet" else pd.read_pickle(path)
        df = df.copy()
        df.index = pd.to_datetime(df.index, utc=True)
        df = df.sort_index()
        df = df.loc[(df.index >= pd.Timestamp(start, tz="UTC")) & (df.index <= _end_ts(end))]
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df
    return pd.DataFrame()


def _attach_funding(df: pd.DataFrame, symbol: str, deriv_run_id: str) -> pd.DataFrame:
    funding = _load_derivative(symbol, deriv_run_id, "funding")
    out = df.copy()
    out["funding_rate"] = _align_last(funding.get("funding_rate") if not funding.empty else pd.Series(dtype=float), out.index)
    out["funding_rate"] = out["funding_rate"].fillna(0.0)
    return out


def _extra_features_for_symbol(df_1h: pd.DataFrame, symbol: str, deriv_run_id: str, snapshot_run_id: str) -> pd.DataFrame:
    idx = pd.DatetimeIndex(df_1h.index)
    out = pd.DataFrame(index=idx)
    out["m5_rv_1h"] = _intrabar_rv(symbol, "5m", idx)
    out["m15_rv_4h"] = _higher_tf_rv(symbol, "15m", idx, bars=16)
    out["m15_ret_4h"] = _higher_tf_ret(symbol, "15m", idx, bars=16)
    out["h4_ret_1"] = _higher_tf_ret(symbol, "4h", idx, bars=1)
    out["h4_ret_6"] = _higher_tf_ret(symbol, "4h", idx, bars=6)
    out["d1_ret_1"] = _higher_tf_ret(symbol, "1d", idx, bars=1)
    out["d1_ret_7"] = _higher_tf_ret(symbol, "1d", idx, bars=7)

    oi = _load_derivative(symbol, deriv_run_id, "open_interest")
    out["oi_value"] = _align_last(_numeric_col(oi, "open_interest_value"), idx)
    out["oi_quote_volume"] = _align_last(_numeric_col(oi, "quote_volume"), idx)
    out["oi_chg_1h"] = out["oi_value"].pct_change()
    out["oi_chg_24h"] = out["oi_value"].pct_change(24)
    out["oi_z_24"] = _rolling_z(out["oi_value"], 24)

    long_short = _load_derivative(symbol, deriv_run_id, "long_short")
    out["ls_ratio"] = _align_last(_numeric_col(long_short, "long_short_ratio"), idx)
    out["ls_chg_1h"] = out["ls_ratio"].pct_change()
    out["ls_chg_24h"] = out["ls_ratio"].pct_change(24)
    out["ls_z_24"] = _rolling_z(out["ls_ratio"], 24)

    static = _snapshot_features(symbol, snapshot_run_id)
    listed_time = static.pop("listed_time", None)
    for col, value in static.items():
        out[col] = value
    if listed_time is not None and pd.notna(listed_time):
        listed = pd.Timestamp(listed_time)
        out["listing_age_days"] = (idx - listed).total_seconds() / 86400.0
    else:
        out["listing_age_days"] = np.nan

    out["symbol"] = symbol
    out = out.set_index("symbol", append=True)
    out.index.names = ["timestamp", "symbol"]
    return out.replace([np.inf, -np.inf], np.nan)


def _load_derivative(symbol: str, run_id: str, kind: str) -> pd.DataFrame:
    path = BASE_DIR / "data" / "derivatives_structure" / run_id / _safe_symbol(symbol) / f"{kind}_5m.parquet"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_parquet(path)
    df.index = pd.to_datetime(df.index, utc=True)
    return df.sort_index()


def _intrabar_rv(symbol: str, timeframe: str, target_idx: pd.DatetimeIndex) -> pd.Series:
    df = _load_ohlcv(symbol, timeframe, str(target_idx.min().date()), str(target_idx.max().date()))
    if df.empty:
        return pd.Series(index=target_idx, dtype=float)
    ret = pd.to_numeric(df["close"], errors="coerce").pct_change()
    return ret.resample("1h").std().reindex(target_idx)


def _higher_tf_ret(symbol: str, timeframe: str, target_idx: pd.DatetimeIndex, bars: int) -> pd.Series:
    df = _load_ohlcv(symbol, timeframe, str(target_idx.min().date()), str(target_idx.max().date()))
    if df.empty:
        return pd.Series(index=target_idx, dtype=float)
    ret = pd.to_numeric(df["close"], errors="coerce").pct_change(bars)
    return _align_last(ret, target_idx)


def _higher_tf_rv(symbol: str, timeframe: str, target_idx: pd.DatetimeIndex, bars: int) -> pd.Series:
    df = _load_ohlcv(symbol, timeframe, str(target_idx.min().date()), str(target_idx.max().date()))
    if df.empty:
        return pd.Series(index=target_idx, dtype=float)
    ret = pd.to_numeric(df["close"], errors="coerce").pct_change()
    rv = ret.rolling(bars, min_periods=max(3, bars // 3)).std()
    return _align_last(rv, target_idx)


def _snapshot_features(symbol: str, snapshot_run_id: str) -> dict[str, Any]:
    base = BASE_DIR / "data" / "derivatives_structure" / snapshot_run_id / _safe_symbol(symbol)
    out: dict[str, Any] = {}
    instrument = _read_first(base / "instrument_snapshot.parquet")
    for col in ("contract_size", "min_amount", "price_tick", "amount_tick", "max_leverage", "listed_time"):
        if col in instrument:
            out[col] = instrument[col]
    return out


def _read_first(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    df = pd.read_parquet(path)
    if df.empty:
        return {}
    return df.iloc[0].to_dict()


def _attach_quality_flags(features: pd.DataFrame, quality: pd.DataFrame, min_train_1h_rows: int) -> pd.DataFrame:
    out = features.copy()
    q = quality.set_index("symbol")
    for symbol in out.index.get_level_values("symbol").unique():
        if symbol not in q.index:
            continue
        mask = out.index.get_level_values("symbol") == symbol
        out.loc[mask, "raw_1h_rows"] = float(q.loc[symbol].get("1h_rows", 0))
        out.loc[mask, "train_eligible_90d"] = float(bool(q.loc[symbol].get("train_eligible_90d", False)))
        out.loc[mask, "train_eligible_180d"] = float(bool(q.loc[symbol].get("train_eligible_180d", False)))
        out.loc[mask, "sample_age_frac_90d"] = min(1.0, float(q.loc[symbol].get("1h_rows", 0) or 0) / float(min_train_1h_rows))
    return out


def _extra_feature_registry() -> dict[str, FeatureSpec]:
    specs: dict[str, FeatureSpec] = {}
    for name, family, source, lookback, desc in [
        ("m5_rv_1h", "micro_price", ["5m close"], 12, "Within-hour realized volatility from 5m bars."),
        ("m15_rv_4h", "micro_price", ["15m close"], 16, "Four-hour realized volatility from 15m bars."),
        ("m15_ret_4h", "momentum", ["15m close"], 16, "Four-hour return from 15m bars."),
        ("h4_ret_1", "regime", ["4h close"], 4, "Latest 4h return aligned to 1h."),
        ("h4_ret_6", "regime", ["4h close"], 24, "24h return from 4h bars aligned to 1h."),
        ("d1_ret_1", "regime", ["1d close"], 24, "Daily return aligned to 1h."),
        ("d1_ret_7", "regime", ["1d close"], 24 * 7, "Seven-day return aligned to 1h."),
        ("oi_value", "derivatives", ["open_interest"], 1, "Latest open interest value."),
        ("oi_quote_volume", "derivatives", ["open_interest"], 1, "Latest OI endpoint quote volume."),
        ("oi_chg_1h", "derivatives", ["open_interest"], 1, "One-hour OI value change."),
        ("oi_chg_24h", "derivatives", ["open_interest"], 24, "24-hour OI value change."),
        ("oi_z_24", "derivatives", ["open_interest"], 24, "24-bar OI z-score."),
        ("ls_ratio", "crowding", ["long_short"], 1, "Latest OKX long-short account ratio."),
        ("ls_chg_1h", "crowding", ["long_short"], 1, "One-hour long-short ratio change."),
        ("ls_chg_24h", "crowding", ["long_short"], 24, "24-hour long-short ratio change."),
        ("ls_z_24", "crowding", ["long_short"], 24, "24-bar long-short ratio z-score."),
        ("contract_size", "instrument", ["instrument"], 0, "Contract size."),
        ("min_amount", "instrument", ["instrument"], 0, "Minimum amount."),
        ("price_tick", "instrument", ["instrument"], 0, "Price tick."),
        ("amount_tick", "instrument", ["instrument"], 0, "Amount tick."),
        ("max_leverage", "instrument", ["instrument"], 0, "Maximum leverage."),
        ("listing_age_days", "instrument", ["instrument"], 0, "Instrument age at timestamp."),
        ("raw_1h_rows", "quality", ["quality"], 0, "Available 1h raw rows for the symbol."),
        ("train_eligible_90d", "quality", ["quality"], 0, "Whether symbol has at least 90 days of 1h rows."),
        ("train_eligible_180d", "quality", ["quality"], 0, "Whether symbol has at least 180 days of 1h rows."),
        ("sample_age_frac_90d", "quality", ["quality"], 0, "Available 1h sample fraction capped at 90 days."),
    ]:
        specs[name] = FeatureSpec(name, family, source, lookback, "1h", True, min(lookback, 24), desc)
    return specs


def _numeric_col(df: pd.DataFrame, col: str) -> pd.Series:
    if df.empty or col not in df:
        return pd.Series(dtype=float)
    return pd.to_numeric(df[col], errors="coerce")


def _align_last(series: pd.Series, target_idx: pd.DatetimeIndex) -> pd.Series:
    if series.empty:
        return pd.Series(index=target_idx, dtype=float)
    source = pd.Series(pd.to_numeric(series, errors="coerce").to_numpy(), index=pd.to_datetime(series.index, utc=True)).sort_index()
    combined = source.index.union(target_idx).sort_values()
    return source.reindex(combined).ffill().reindex(target_idx)


def _rolling_z(series: pd.Series, window: int) -> pd.Series:
    mean = series.rolling(window, min_periods=max(3, window // 3)).mean()
    std = series.rolling(window, min_periods=max(3, window // 3)).std()
    return (series - mean) / std.replace(0, np.nan)


def _write_frame(df: pd.DataFrame, path: Path) -> None:
    try:
        df.to_parquet(path)
    except Exception:
        df.to_pickle(path.with_suffix(".pkl"))


def _safe_symbol(symbol: str) -> str:
    return symbol.replace("/", "_").replace(":", "_")


def _end_ts(end: str) -> pd.Timestamp:
    return pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1) - pd.Timedelta(milliseconds=1)


if __name__ == "__main__":
    raise SystemExit(main())
