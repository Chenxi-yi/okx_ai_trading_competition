#!/usr/bin/env python3
"""Study how BTC regimes affect alt forward returns for C-Auto."""

from __future__ import annotations

import argparse
import json
import shutil
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


DEFAULT_PLAN = ROOT / ".claude" / "knowledge" / "research" / "c_auto_btc_impact_plan.md"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run BTC -> alt impact study from a C-Auto feature dataset")
    p.add_argument("--dataset-id", default="c_auto_feature_store_v1")
    p.add_argument("--experiment-id", default="btc_alt_impact_v1")
    p.add_argument("--plan-path", default=str(DEFAULT_PLAN))
    p.add_argument("--label-horizons", default="1,3,6,12,24")
    p.add_argument("--min-symbol-rows", type=int, default=500)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    dataset_dir = BASE_DIR / "data" / "features" / args.dataset_id
    out_dir = BASE_DIR / "data" / "research" / "c_auto" / args.experiment_id
    out_dir.mkdir(parents=True, exist_ok=True)

    features = _read_frame(dataset_dir, "features")
    labels = _read_frame(dataset_dir, "labels")
    if features.empty or labels.empty:
        raise RuntimeError(f"Empty feature/label dataset: {dataset_dir}")

    horizons = tuple(int(x.strip()) for x in args.label_horizons.split(",") if x.strip())
    btc_state = _build_btc_state(features)
    panel = _build_alt_panel(features, labels, btc_state, horizons)
    if panel.empty:
        raise RuntimeError("No alt panel rows after joining BTC state and labels")

    regime_summary = _regime_summary(panel, horizons)
    bucket_summary = _btc_return_bucket_summary(panel, horizons)
    symbol_beta = _symbol_beta(panel, min_rows=args.min_symbol_rows)
    symbol_regime = _symbol_regime_summary(panel, horizons)
    recommendations = _recommendations(regime_summary, bucket_summary)

    _write_frame(regime_summary, out_dir / "regime_summary.parquet")
    _write_frame(bucket_summary, out_dir / "btc_return_bucket_summary.parquet")
    _write_frame(symbol_beta, out_dir / "symbol_beta.parquet")
    _write_frame(symbol_regime, out_dir / "symbol_regime_summary.parquet")
    _write_frame(btc_state, out_dir / "btc_regime_timeline.parquet")
    regime_summary.to_csv(out_dir / "regime_summary.csv", index=False)
    bucket_summary.to_csv(out_dir / "btc_return_bucket_summary.csv", index=False)
    symbol_beta.to_csv(out_dir / "symbol_beta.csv", index=False)
    symbol_regime.to_csv(out_dir / "symbol_regime_summary.csv", index=False)

    plan_path = Path(args.plan_path)
    if plan_path.exists():
        shutil.copyfile(plan_path, out_dir / "experiment_plan.md")

    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "experiment_id": args.experiment_id,
        "dataset_id": args.dataset_id,
        "dataset_dir": str(dataset_dir),
        "plan_path": str(plan_path),
        "horizons": list(horizons),
        "shape": {
            "features_rows": int(len(features)),
            "labels_rows": int(len(labels)),
            "panel_rows": int(len(panel)),
            "symbols": int(panel.index.get_level_values("symbol").nunique()),
            "btc_state_rows": int(len(btc_state)),
        },
        "artifacts": {
            "regime_summary": "regime_summary.parquet",
            "btc_return_bucket_summary": "btc_return_bucket_summary.parquet",
            "symbol_beta": "symbol_beta.parquet",
            "symbol_regime_summary": "symbol_regime_summary.parquet",
            "btc_regime_timeline": "btc_regime_timeline.parquet",
            "experiment_plan": "experiment_plan.md" if (out_dir / "experiment_plan.md").exists() else None,
        },
        "recommendations": recommendations,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    (out_dir / "recommendations.json").write_text(json.dumps(recommendations, indent=2, sort_keys=True))

    print(json.dumps(manifest, indent=2, sort_keys=True))
    print("\nRegime summary:")
    print(regime_summary.to_string(index=False))
    print("\nTop BTC beta symbols:")
    print(symbol_beta.sort_values("beta_1h", ascending=False).head(15).to_string(index=False))
    return 0


def _read_frame(dataset_dir: Path, stem: str) -> pd.DataFrame:
    parquet = dataset_dir / f"{stem}.parquet"
    pkl = dataset_dir / f"{stem}.pkl"
    if parquet.exists():
        return pd.read_parquet(parquet)
    if pkl.exists():
        return pd.read_pickle(pkl)
    raise FileNotFoundError(f"Missing {stem}.parquet or {stem}.pkl in {dataset_dir}")


def _build_btc_state(features: pd.DataFrame) -> pd.DataFrame:
    btc = features.xs("BTC/USDT", level="symbol").sort_index()
    close = pd.to_numeric(btc["close"], errors="coerce")
    ret_1h = close.pct_change(1)
    ret_4h = close.pct_change(4)
    ret_24h = close.pct_change(24)
    ret_7d = close.pct_change(24 * 7)
    ret_30d = close.pct_change(24 * 30)
    ema_20d = close.ewm(span=24 * 20, min_periods=24 * 5, adjust=False).mean()
    ema_60d = close.ewm(span=24 * 60, min_periods=24 * 10, adjust=False).mean()
    rv_24h = ret_1h.rolling(24, min_periods=8).std()
    rv_7d = ret_1h.rolling(24 * 7, min_periods=24).std()
    high_30d = close.rolling(24 * 30, min_periods=24 * 7).max()
    drawdown_30d = close / high_30d - 1.0

    out = pd.DataFrame(
        {
            "btc_close": close,
            "btc_ret_1h": ret_1h,
            "btc_ret_4h": ret_4h,
            "btc_ret_24h": ret_24h,
            "btc_ret_7d": ret_7d,
            "btc_ret_30d": ret_30d,
            "btc_ema_20d": ema_20d,
            "btc_ema_60d": ema_60d,
            "btc_ema20_gt_ema60": (ema_20d > ema_60d).astype(float),
            "btc_above_ema20": (close > ema_20d).astype(float),
            "btc_above_ema60": (close > ema_60d).astype(float),
            "btc_rv_24h": rv_24h,
            "btc_rv_7d": rv_7d,
            "btc_drawdown_30d": drawdown_30d,
        },
        index=btc.index,
    )
    out["btc_regime_6"] = _classify_regime(out)
    out["btc_regime_3"] = out["btc_regime_6"].map(
        {
            "deep_bear": "risk_off",
            "bear": "risk_off",
            "chop_short": "neutral",
            "chop_long": "neutral",
            "bull": "risk_on",
            "strong_bull": "risk_on",
        }
    )
    out.index.name = "timestamp"
    return out


def _classify_regime(state: pd.DataFrame) -> pd.Series:
    high_vol = state["btc_rv_7d"] >= state["btc_rv_7d"].rolling(24 * 90, min_periods=24 * 14).quantile(0.75)
    below_both = (state["btc_above_ema20"] < 0.5) & (state["btc_above_ema60"] < 0.5)
    above_both = (state["btc_above_ema20"] > 0.5) & (state["btc_above_ema60"] > 0.5)
    regime = pd.Series("chop_short", index=state.index, dtype=object)

    deep_bear = (
        (state["btc_ret_30d"] <= -0.20)
        | (state["btc_drawdown_30d"] <= -0.25)
        | (below_both & high_vol & (state["btc_ret_7d"] < 0))
    )
    bear = below_both & ((state["btc_ret_7d"] < 0) | (state["btc_ret_30d"] < 0))
    strong_bull = (state["btc_ret_30d"] >= 0.20) & above_both & (state["btc_drawdown_30d"] >= -0.08)
    bull = above_both & ((state["btc_ret_7d"] > 0) | (state["btc_ret_30d"] > 0))
    chop_long = ~deep_bear & ~bear & ~strong_bull & ~bull & (state["btc_above_ema20"] > 0.5)

    regime.loc[chop_long] = "chop_long"
    regime.loc[bull] = "bull"
    regime.loc[strong_bull] = "strong_bull"
    regime.loc[bear] = "bear"
    regime.loc[deep_bear] = "deep_bear"
    return regime


def _build_alt_panel(features: pd.DataFrame, labels: pd.DataFrame, btc_state: pd.DataFrame, horizons: tuple[int, ...]) -> pd.DataFrame:
    label_cols = [f"fwd_ret_net_long_{h}" for h in horizons if f"fwd_ret_net_long_{h}" in labels.columns]
    alt = features[["close"]].join(labels[label_cols], how="inner")
    alt = alt.loc[alt.index.get_level_values("symbol") != "BTC/USDT"].copy()
    alt_close = pd.to_numeric(alt["close"], errors="coerce")
    alt["alt_ret_1h"] = alt_close.groupby(level="symbol").pct_change(1)
    alt["alt_ret_4h"] = alt_close.groupby(level="symbol").pct_change(4)
    state = btc_state.reindex(alt.index.get_level_values("timestamp")).set_index(alt.index)
    return pd.concat([alt, state], axis=1).replace([np.inf, -np.inf], np.nan)


def _regime_summary(panel: pd.DataFrame, horizons: tuple[int, ...]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for regime, group in panel.groupby("btc_regime_6", dropna=True):
        row: dict[str, Any] = _base_group_stats(group)
        row["btc_regime_6"] = regime
        row["btc_regime_3"] = str(group["btc_regime_3"].dropna().iloc[0]) if group["btc_regime_3"].notna().any() else ""
        for horizon in horizons:
            col = f"fwd_ret_net_long_{horizon}"
            if col in group:
                _add_return_stats(row, group[col], f"alt_fwd_{horizon}h")
        rows.append(row)
    out = pd.DataFrame(rows)
    return out.sort_values("alt_fwd_24h_mean", ascending=False) if "alt_fwd_24h_mean" in out else out


def _btc_return_bucket_summary(panel: pd.DataFrame, horizons: tuple[int, ...]) -> pd.DataFrame:
    df = panel.dropna(subset=["btc_ret_24h"]).copy()
    df["btc_ret_24h_bucket"] = pd.qcut(df["btc_ret_24h"], q=7, duplicates="drop")
    rows: list[dict[str, Any]] = []
    for bucket, group in df.groupby("btc_ret_24h_bucket", observed=True):
        row: dict[str, Any] = _base_group_stats(group)
        row["btc_ret_24h_bucket"] = str(bucket)
        row["btc_ret_24h_min"] = float(group["btc_ret_24h"].min())
        row["btc_ret_24h_max"] = float(group["btc_ret_24h"].max())
        for horizon in horizons:
            col = f"fwd_ret_net_long_{horizon}"
            if col in group:
                _add_return_stats(row, group[col], f"alt_fwd_{horizon}h")
        rows.append(row)
    return pd.DataFrame(rows)


def _symbol_beta(panel: pd.DataFrame, min_rows: int) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for symbol, group in panel.groupby(level="symbol", sort=False):
        g = group[["alt_ret_1h", "btc_ret_1h", "fwd_ret_net_long_24"]].dropna()
        if len(g) < min_rows:
            continue
        btc_var = float(g["btc_ret_1h"].var())
        beta = float(g["alt_ret_1h"].cov(g["btc_ret_1h"]) / btc_var) if btc_var > 0 else 0.0
        corr = _safe_corr(g["alt_ret_1h"], g["btc_ret_1h"])
        rows.append(
            {
                "symbol": symbol,
                "rows": int(len(g)),
                "beta_1h": beta,
                "corr_1h": corr,
                "mean_fwd_24h": _safe_mean(g["fwd_ret_net_long_24"]),
                "hit_rate_24h": _hit_rate(g["fwd_ret_net_long_24"]),
            }
        )
    return pd.DataFrame(rows).sort_values("beta_1h", ascending=False)


def _symbol_regime_summary(panel: pd.DataFrame, horizons: tuple[int, ...]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for (symbol, regime), group in panel.groupby([panel.index.get_level_values("symbol"), "btc_regime_6"], sort=False):
        row: dict[str, Any] = {"symbol": symbol, "btc_regime_6": regime, "rows": int(len(group))}
        for horizon in horizons:
            col = f"fwd_ret_net_long_{horizon}"
            if col in group:
                row[f"fwd_{horizon}h_mean"] = _safe_mean(group[col])
                row[f"fwd_{horizon}h_hit_rate"] = _hit_rate(group[col])
        rows.append(row)
    return pd.DataFrame(rows)


def _base_group_stats(group: pd.DataFrame) -> dict[str, Any]:
    return {
        "rows": int(len(group)),
        "symbols": int(group.index.get_level_values("symbol").nunique()),
        "timestamps": int(group.index.get_level_values("timestamp").nunique()),
        "btc_ret_24h_mean": _safe_mean(group["btc_ret_24h"]),
        "btc_ret_7d_mean": _safe_mean(group["btc_ret_7d"]),
        "btc_rv_24h_mean": _safe_mean(group["btc_rv_24h"]),
        "btc_drawdown_30d_mean": _safe_mean(group["btc_drawdown_30d"]),
    }


def _add_return_stats(row: dict[str, Any], series: pd.Series, prefix: str) -> None:
    s = pd.to_numeric(series, errors="coerce").dropna()
    row[f"{prefix}_mean"] = _safe_mean(s)
    row[f"{prefix}_median"] = float(s.median()) if len(s) else 0.0
    row[f"{prefix}_hit_rate"] = _hit_rate(s)
    row[f"{prefix}_q20"] = float(s.quantile(0.20)) if len(s) else 0.0
    row[f"{prefix}_q80"] = float(s.quantile(0.80)) if len(s) else 0.0
    row[f"{prefix}_dispersion_q80_q20"] = row[f"{prefix}_q80"] - row[f"{prefix}_q20"]


def _recommendations(regime_summary: pd.DataFrame, bucket_summary: pd.DataFrame) -> dict[str, Any]:
    rec: dict[str, Any] = {"regime_bias": {}, "notes": []}
    if "alt_fwd_24h_mean" in regime_summary:
        for _, row in regime_summary.iterrows():
            mean = float(row["alt_fwd_24h_mean"])
            hit = float(row.get("alt_fwd_24h_hit_rate", 0.0))
            if mean > 0.001 and hit > 0.52:
                bias = "long_bias"
            elif mean < -0.001 and hit < 0.48:
                bias = "short_or_avoid"
            else:
                bias = "selective_or_neutral"
            rec["regime_bias"][str(row["btc_regime_6"])] = {
                "bias": bias,
                "alt_fwd_24h_mean": mean,
                "hit_rate": hit,
                "rows": int(row["rows"]),
            }
    rec["notes"].append("Use regime results as research priors; do not trade without delayed-execution backtest.")
    rec["notes"].append("Current universe is survivorship-biased; listing-aware masks remain required.")
    return rec


def _safe_mean(series: pd.Series) -> float:
    value = pd.to_numeric(series, errors="coerce").mean()
    return float(value) if np.isfinite(value) else 0.0


def _safe_corr(a: pd.Series, b: pd.Series) -> float:
    value = pd.to_numeric(a, errors="coerce").corr(pd.to_numeric(b, errors="coerce"))
    return float(value) if value is not None and np.isfinite(value) else 0.0


def _hit_rate(series: pd.Series) -> float:
    s = pd.to_numeric(series, errors="coerce").dropna()
    return float((s > 0).mean()) if len(s) else 0.0


def _write_frame(df: pd.DataFrame, path: Path) -> None:
    try:
        df.to_parquet(path)
    except Exception:
        df.to_pickle(path.with_suffix(".pkl"))


if __name__ == "__main__":
    raise SystemExit(main())
