#!/usr/bin/env python3
"""Research OI-up / price-compression breakout behavior on the C-Auto feature store."""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
FEATURE_DIR = ROOT / "engine" / "data" / "features"
OUT_ROOT = ROOT / "engine" / "data" / "research" / "oi_pressure"
DERIV_ROOT = ROOT / "engine" / "data" / "derivatives_structure"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Research OI compression breakout signals")
    p.add_argument("--dataset-id", default="c_auto_feature_store_rebuild_161_ohlcv_snapshot_v1")
    p.add_argument("--oi-run-id", default="rebuild_161_open_interest_1h_20230101_20260507")
    p.add_argument("--min-listing-days", type=float, default=60.0)
    p.add_argument("--min-volume-usd", type=float, default=200_000.0)
    p.add_argument("--oi-quantile", type=float, default=0.85)
    p.add_argument("--compression-quantile", type=float, default=0.35)
    p.add_argument("--out-id", default=None)
    return p.parse_args()


def load_features(dataset_id: str) -> pd.DataFrame:
    path = FEATURE_DIR / dataset_id / "features.parquet"
    if not path.exists():
        raise FileNotFoundError(path)
    cols = [
        "close",
        "volume_usd",
        "ret_1",
        "ret_3",
        "ret_6",
        "ret_12",
        "ret_24",
        "range_pct",
        "rv_12",
        "rv_24",
        "oi_value",
        "oi_chg_1h",
        "oi_chg_24h",
        "oi_z_24",
        "ls_ratio",
        "funding_rate",
        "listing_age_days",
        "btc_regime_3",
        "btc_regime_6",
    ]
    df = pd.read_parquet(path, columns=[c for c in cols if c])
    if not isinstance(df.index, pd.MultiIndex):
        raise ValueError("feature store must be indexed by timestamp,symbol")
    return df.sort_index()


def load_open_interest(run_id: str) -> pd.DataFrame:
    run_dir = DERIV_ROOT / run_id
    paths = sorted(run_dir.glob("*/open_interest_1h.parquet"))
    if not paths:
        raise FileNotFoundError(f"no open_interest_1h.parquet files under {run_dir}")
    frames = []
    for path in paths:
        try:
            item = pd.read_parquet(path, columns=["symbol", "open_interest_value"])
        except Exception:
            continue
        if item.empty:
            continue
        item = item.reset_index()
        item["timestamp"] = pd.to_datetime(item["timestamp"], utc=True)
        item["symbol"] = item["symbol"].astype(str)
        item["oi_value_external"] = pd.to_numeric(item["open_interest_value"], errors="coerce")
        frames.append(item[["timestamp", "symbol", "oi_value_external"]])
    if not frames:
        return pd.DataFrame(columns=["oi_value_external", "oi_chg_24h_external", "oi_z_24_external"])
    oi = pd.concat(frames, ignore_index=True).dropna(subset=["oi_value_external"])
    oi = oi.sort_values(["symbol", "timestamp"])
    grouped = oi.groupby("symbol")["oi_value_external"]
    oi["oi_chg_24h_external"] = grouped.pct_change(24)
    rolling_mean = grouped.transform(lambda s: s.rolling(24, min_periods=12).mean())
    rolling_std = grouped.transform(lambda s: s.rolling(24, min_periods=12).std())
    oi["oi_z_24_external"] = (oi["oi_value_external"] - rolling_mean) / rolling_std.replace(0, np.nan)
    return oi.set_index(["timestamp", "symbol"]).sort_index()


def prepare(df: pd.DataFrame, oi: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    out = df.copy()
    if not oi.empty:
        out = out.join(oi, how="left")
        if "oi_value" not in out.columns or out["oi_value"].notna().sum() == 0:
            out["oi_value"] = out["oi_value_external"]
        if "oi_chg_24h" not in out.columns or out["oi_chg_24h"].notna().sum() == 0:
            out["oi_chg_24h"] = out["oi_chg_24h_external"]
        if "oi_z_24" not in out.columns or out["oi_z_24"].notna().sum() == 0:
            out["oi_z_24"] = out["oi_z_24_external"]
    for col in ["close", "volume_usd", "oi_value", "oi_chg_24h", "ret_1", "ret_3", "ret_6", "ret_12", "ret_24", "range_pct", "rv_12", "rv_24"]:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    symbols = out.index.get_level_values("symbol")
    grouped_close = out["close"].groupby(symbols)
    for horizon in (6, 12, 24):
        future_close = grouped_close.shift(-horizon)
        out[f"fwd_ret_{horizon}h"] = future_close / out["close"] - 1.0
        out[f"fwd_abs_{horizon}h"] = out[f"fwd_ret_{horizon}h"].abs()
        out[f"dir_mom3_ret_{horizon}h"] = np.sign(out["ret_3"].fillna(0.0)) * out[f"fwd_ret_{horizon}h"]
    out = out[
        (out["listing_age_days"].fillna(0.0) >= args.min_listing_days)
        & (out["volume_usd"].fillna(0.0) >= args.min_volume_usd)
        & out["oi_chg_24h"].notna()
        & out["ret_24"].notna()
        & out["rv_24"].notna()
    ].copy()
    return out


def build_signal(df: pd.DataFrame, args: argparse.Namespace) -> tuple[pd.DataFrame, dict[str, float]]:
    oi_cut = float(df["oi_chg_24h"].quantile(args.oi_quantile))
    abs_ret_cut = float(df["ret_24"].abs().quantile(args.compression_quantile))
    rv_cut = float(df["rv_24"].quantile(args.compression_quantile))
    range_cut = float(df["range_pct"].rolling(24, min_periods=6).mean().quantile(args.compression_quantile))
    range_mean_24 = df["range_pct"].groupby(df.index.get_level_values("symbol")).rolling(24, min_periods=6).mean().reset_index(level=0, drop=True)
    signal = (
        (df["oi_chg_24h"] >= oi_cut)
        & (df["ret_24"].abs() <= abs_ret_cut)
        & (df["rv_24"] <= rv_cut)
        & (range_mean_24 <= range_cut)
    )
    out = df.copy()
    out["oi_pressure_breakout_candidate"] = signal
    out["range_mean_24"] = range_mean_24
    return out, {
        "oi_chg_24h_cut": oi_cut,
        "abs_ret_24h_cut": abs_ret_cut,
        "rv_24h_cut": rv_cut,
        "range_mean_24h_cut": range_cut,
    }


def summarize(df: pd.DataFrame, signal_col: str) -> dict[str, object]:
    signal_df = df[df[signal_col]].copy()
    baseline = df.copy()
    summary: dict[str, object] = {
        "rows": int(len(df)),
        "symbols": int(df.index.get_level_values("symbol").nunique()),
        "events": int(len(signal_df)),
        "event_symbols": int(signal_df.index.get_level_values("symbol").nunique()) if len(signal_df) else 0,
    }
    horizons = {}
    for horizon in (6, 12, 24):
        fwd_abs = f"fwd_abs_{horizon}h"
        fwd_ret = f"fwd_ret_{horizon}h"
        dir_col = f"dir_mom3_ret_{horizon}h"
        threshold = float(baseline[fwd_abs].quantile(0.75))
        event_abs = float(signal_df[fwd_abs].mean()) if len(signal_df) else math.nan
        base_abs = float(baseline[fwd_abs].mean())
        event_breakout = float((signal_df[fwd_abs] >= threshold).mean()) if len(signal_df) else math.nan
        base_breakout = float((baseline[fwd_abs] >= threshold).mean())
        event_dir = float(signal_df[dir_col].mean()) if len(signal_df) else math.nan
        base_dir = float(baseline[dir_col].mean())
        event_up = float((signal_df[fwd_ret] > 0).mean()) if len(signal_df) else math.nan
        horizons[f"{horizon}h"] = {
            "breakout_threshold_abs_ret": threshold,
            "event_mean_abs_ret": event_abs,
            "baseline_mean_abs_ret": base_abs,
            "event_breakout_rate": event_breakout,
            "baseline_breakout_rate": base_breakout,
            "event_mom3_directional_ret": event_dir,
            "baseline_mom3_directional_ret": base_dir,
            "event_up_rate": event_up,
        }
    summary["horizons"] = horizons
    if len(signal_df):
        by_regime = (
            signal_df.groupby("btc_regime_3")["fwd_abs_12h"]
            .agg(["count", "mean"])
            .sort_values("count", ascending=False)
            .head(12)
            .reset_index()
            .to_dict(orient="records")
        )
    else:
        by_regime = []
    summary["by_btc_regime_3"] = by_regime
    return summary


def write_outputs(df: pd.DataFrame, summary: dict[str, object], cuts: dict[str, float], args: argparse.Namespace) -> Path:
    out_id = args.out_id or "oi_pressure_breakout_v1"
    out_dir = OUT_ROOT / out_id
    out_dir.mkdir(parents=True, exist_ok=True)
    signal_df = df[df["oi_pressure_breakout_candidate"]].copy()
    sample = signal_df.reset_index().sort_values("timestamp").tail(500)
    keep_cols = [
        "timestamp",
        "symbol",
        "close",
        "volume_usd",
        "oi_chg_24h",
        "ret_24",
        "rv_24",
        "range_mean_24",
        "btc_regime_3",
        "fwd_abs_6h",
        "fwd_abs_12h",
        "fwd_abs_24h",
        "dir_mom3_ret_12h",
    ]
    sample[[col for col in keep_cols if col in sample.columns]].to_csv(out_dir / "events_tail.csv", index=False)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset_id": args.dataset_id,
        "oi_run_id": args.oi_run_id,
        "parameters": vars(args),
        "cuts": cuts,
        "summary": summary,
    }
    (out_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n")
    lines = [
        "# OI Pressure Breakout Research v1",
        "",
        f"- Dataset: `{args.dataset_id}`",
        f"- Rows: `{summary['rows']}`",
        f"- Symbols: `{summary['symbols']}`",
        f"- Events: `{summary['events']}` across `{summary['event_symbols']}` symbols",
        f"- OI 24h cut: `{cuts['oi_chg_24h_cut']:.4f}`",
        "",
        "## Horizon Summary",
        "",
        "| Horizon | Event AbsRet | Base AbsRet | Event Breakout | Base Breakout | Mom3 DirRet | Up Rate |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for horizon, row in summary["horizons"].items():
        lines.append(
            "| "
            + " | ".join(
                [
                    horizon,
                    f"{row['event_mean_abs_ret'] * 100:.2f}%",
                    f"{row['baseline_mean_abs_ret'] * 100:.2f}%",
                    f"{row['event_breakout_rate'] * 100:.2f}%",
                    f"{row['baseline_breakout_rate'] * 100:.2f}%",
                    f"{row['event_mom3_directional_ret'] * 100:.3f}%",
                    f"{row['event_up_rate'] * 100:.2f}%",
                ]
            )
            + " |"
        )
    lines += [
        "",
        "## Initial Read",
        "",
        "- This is a breakout detector, not yet a standalone directional strategy.",
        "- Directional edge is estimated with a simple 3h momentum sign; it needs a gate before paper trading.",
        "- Next step: turn candidates into a committee signal only when event breakout uplift and directional edge are positive in the active BTC regime.",
    ]
    (out_dir / "README.md").write_text("\n".join(lines) + "\n")
    return out_dir


def main() -> int:
    args = parse_args()
    df = prepare(load_features(args.dataset_id), load_open_interest(args.oi_run_id), args)
    df, cuts = build_signal(df, args)
    summary = summarize(df, "oi_pressure_breakout_candidate")
    out_dir = write_outputs(df, summary, cuts, args)
    print(out_dir)
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
