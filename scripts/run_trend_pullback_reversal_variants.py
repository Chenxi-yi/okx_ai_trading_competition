#!/usr/bin/env python3
"""Compare 4h-trend / 1h-pullback reversal entry variants.

Variants:
  1. baseline: enter every raw pattern candidate.
  2. quality_top30: enter only the top 30% hand-scored candidates per scan.
  3. rank_topN: enter only the top 1/2/3 hand-scored candidates per scan.
  4. rolling_cluster: learn historical candidate clusters with a point-in-time
     rolling fit, then trade only clusters with good trailing performance.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "engine" / "data"
FEATURE_DIR = DATA_DIR / "features"
OUT_ROOT = DATA_DIR / "research" / "trend_pullback_reversal"


FEATURE_COLS = [
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
    "rv_24",
    "vol_z_24",
    "trend_eff_24",
    "funding_rate",
    "funding_z_24",
    "oi_chg_24h",
    "oi_z_24",
    "ls_z_24",
    "h4_ret_1",
    "h4_ret_6",
    "btc_ret_1h",
    "btc_ret_4h",
    "btc_ret_24h",
    "btc_rv_24h",
    "btc_rv_7d",
    "btc_drawdown_30d",
    "btc_regime_6",
]

CLUSTER_FEATURES = [
    "h4_trend_abs",
    "h4_trend_align",
    "counter_move",
    "counter_ratio",
    "reversal_ret_abs",
    "reversal_range_frac",
    "close_location_score",
    "atr_14_pct",
    "rv_24",
    "vol_z_24",
    "trend_eff_24",
    "funding_z_24",
    "oi_z_24",
    "ls_z_24",
    "btc_ret_4h",
    "btc_ret_24h",
    "btc_rv_24h",
    "btc_drawdown_30d",
]


@dataclass
class ClusterModel:
    fitted_at: pd.Timestamp
    mean: np.ndarray
    scale: np.ndarray
    centers: np.ndarray
    eligible_clusters: set[int]
    stats: pd.DataFrame


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run trend-pullback reversal variant experiments")
    p.add_argument("--dataset-id", default="c_auto_feature_store_rebuild_161_ohlcv_snapshot_v1")
    p.add_argument("--start", default="2025-01-01")
    p.add_argument("--end", default="")
    p.add_argument("--max-symbols", type=int, default=80)
    p.add_argument("--min-volume-usd", type=float, default=200_000.0)
    p.add_argument("--min-listing-days", type=float, default=60.0)
    p.add_argument("--h4-trend-min", type=float, default=0.0)
    p.add_argument("--h4-countertrend-allow", type=float, default=0.005)
    p.add_argument("--max-countertrend-multiple", type=float, default=4.0)
    p.add_argument("--max-countertrend-move-pct", type=float, default=0.045)
    p.add_argument("--near-extreme-pct", type=float, default=0.003)
    p.add_argument("--loose-extreme-pct", type=float, default=0.006)
    p.add_argument("--trigger-range-frac", type=float, default=0.25)
    p.add_argument("--side-mode", choices=["both", "long", "short"], default="both")
    p.add_argument("--target-pct", type=float, default=0.03)
    p.add_argument("--stop-pct", type=float, default=0.015)
    p.add_argument("--max-hold-hours", type=int, default=12)
    p.add_argument("--fee-bps-per-side", type=float, default=5.0)
    p.add_argument("--slippage-bps-per-side", type=float, default=2.0)
    p.add_argument("--quality-top-frac", type=float, default=0.30)
    p.add_argument("--rank-top-n", default="1,2,3")
    p.add_argument("--cluster-k", type=int, default=6)
    p.add_argument("--cluster-train-days", type=int, default=180)
    p.add_argument("--cluster-refit-hours", type=int, default=24)
    p.add_argument("--cluster-min-train", type=int, default=400)
    p.add_argument("--cluster-min-count", type=int, default=40)
    p.add_argument("--cluster-min-win-rate", type=float, default=0.52)
    p.add_argument("--cluster-min-mean-return", type=float, default=0.0)
    p.add_argument("--out-id", default="")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    out_id = args.out_id or f"trend_pullback_reversal_variants_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    out_dir = OUT_ROOT / out_id
    out_dir.mkdir(parents=True, exist_ok=True)

    candidates = build_candidates(args)
    candidates.to_csv(out_dir / "candidate_events.csv", index=False)
    (out_dir / "experiment_plan.md").write_text(experiment_plan(args))

    variant_frames: dict[str, pd.DataFrame] = {}
    variant_frames["baseline"] = apply_nonoverlap(candidates)
    variant_frames[f"quality_top{int(args.quality_top_frac * 100)}"] = apply_nonoverlap(select_quality_top_frac(candidates, args.quality_top_frac))

    for n in parse_top_ns(args.rank_top_n):
        variant_frames[f"rank_top{n}"] = apply_nonoverlap(select_rank_top_n(candidates, n))

    cluster_trades, cluster_diag, cluster_stats = select_rolling_clusters(candidates, args)
    variant_frames["rolling_cluster"] = apply_nonoverlap(cluster_trades)

    rows = []
    for name, frame in variant_frames.items():
        frame.to_csv(out_dir / f"{name}_trades.csv", index=False)
        summary = summarize(frame, args)
        rows.append({"variant": name, **summary})

    comparison = pd.DataFrame(rows).sort_values(["score", "mean_net_return"], ascending=False)
    comparison.to_csv(out_dir / "comparison.csv", index=False)
    cluster_diag.to_csv(out_dir / "rolling_cluster_assignments.csv", index=False)
    cluster_stats.to_csv(out_dir / "rolling_cluster_refit_stats.csv", index=False)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "args": vars(args),
        "candidate_events": int(len(candidates)),
        "comparison": comparison.to_dict(orient="records"),
        "outputs": {
            "candidate_events": "candidate_events.csv",
            "comparison": "comparison.csv",
            "experiment_plan": "experiment_plan.md",
            "rolling_cluster_assignments": "rolling_cluster_assignments.csv",
            "rolling_cluster_refit_stats": "rolling_cluster_refit_stats.csv",
        },
    }
    (out_dir / "summary.json").write_text(json.dumps(payload, indent=2, sort_keys=True))
    (out_dir / "report.md").write_text(markdown_report(payload, comparison, cluster_stats))
    print(json.dumps({"out_dir": str(out_dir.relative_to(ROOT)), **payload}, indent=2, sort_keys=True))
    return 0


def build_candidates(args: argparse.Namespace) -> pd.DataFrame:
    path = FEATURE_DIR / args.dataset_id / "features.parquet"
    if not path.exists():
        raise FileNotFoundError(path)
    df = pd.read_parquet(path, columns=FEATURE_COLS).sort_index()
    start = pd.Timestamp(args.start, tz="UTC")
    end = pd.Timestamp(args.end, tz="UTC") if args.end else pd.Timestamp.now(tz="UTC")
    ts = df.index.get_level_values("timestamp")
    df = df[(ts >= start) & (ts <= end)].copy()
    for col in FEATURE_COLS:
        if col != "btc_regime_6" and col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df[
        (df["volume_usd"].fillna(0.0) >= float(args.min_volume_usd))
        & (df["listing_age_days"].fillna(0.0) >= float(args.min_listing_days))
        & (df["train_eligible_90d"].fillna(0.0) > 0)
        & df["close"].notna()
        & df["h4_ret_6"].notna()
        & df["h4_ret_1"].notna()
    ].copy()
    latest_volume = df.groupby(level="symbol")["volume_usd"].last().sort_values(ascending=False)
    keep_symbols = set(latest_volume.head(int(args.max_symbols)).index.astype(str))
    df = df[df.index.get_level_values("symbol").astype(str).isin(keep_symbols)].copy()

    grouped = df.groupby(level="symbol")
    horizon = max(1, int(args.max_hold_hours))
    df["fwd_ret"] = grouped["close"].shift(-horizon) / df["close"] - 1.0
    df["median_abs_1h_24"] = grouped["ret_1"].transform(lambda s: s.abs().rolling(24, min_periods=8).median())

    trend_min = abs(float(args.h4_trend_min))
    h4_allow = abs(float(args.h4_countertrend_allow))
    df["side"] = np.where(
        (df["h4_ret_6"] > trend_min) & (df["h4_ret_1"] > -h4_allow),
        "long",
        np.where((df["h4_ret_6"] < -trend_min) & (df["h4_ret_1"] < h4_allow), "short", ""),
    )
    if args.side_mode == "long":
        df.loc[df["side"] != "long", "side"] = ""
    elif args.side_mode == "short":
        df.loc[df["side"] != "short", "side"] = ""

    counter_limit = np.minimum(
        float(args.max_countertrend_move_pct),
        np.maximum(0.008, float(args.max_countertrend_multiple) * df["median_abs_1h_24"]),
    )
    long_pullback = (df["side"] == "long") & (df["ret_3"] < 0) & (df["ret_3"].abs() <= counter_limit)
    short_pullback = (df["side"] == "short") & (df["ret_3"] > 0) & (df["ret_3"].abs() <= counter_limit)
    near = abs(float(args.near_extreme_pct))
    loose = abs(float(args.loose_extreme_pct))
    trigger_frac = abs(float(args.trigger_range_frac))
    long_trigger = (df["ret_1"] > 0) & (
        (df["close_to_high"] >= -near)
        | ((df["ret_1"] > df["range_pct"].abs() * trigger_frac) & (df["close_to_high"] >= -loose))
    )
    short_trigger = (df["ret_1"] < 0) & (
        (df["close_to_low"] <= near)
        | ((df["ret_1"].abs() > df["range_pct"].abs() * trigger_frac) & (df["close_to_low"] <= loose))
    )
    signal = (long_pullback & long_trigger) | (short_pullback & short_trigger)
    events = df[signal & df["fwd_ret"].notna()].copy()

    if events.empty:
        return pd.DataFrame()

    cost = 2.0 * (float(args.fee_bps_per_side) + float(args.slippage_bps_per_side)) / 10000.0
    events["gross_return"] = np.where(events["side"] == "short", -events["fwd_ret"], events["fwd_ret"])
    events["net_return"] = events["gross_return"] - cost
    events["r_multiple"] = events["net_return"] / float(args.stop_pct)
    events["counter_move"] = events["ret_3"].abs()
    events["counter_limit"] = counter_limit.loc[events.index].astype(float)
    events["counter_ratio"] = (events["counter_move"] / events["counter_limit"].replace(0, np.nan)).clip(0, 3)
    events["h4_trend_abs"] = events["h4_ret_6"].abs()
    events["h4_trend_align"] = np.sign(events["h4_ret_6"]) * events["h4_ret_1"]
    events["reversal_ret_abs"] = events["ret_1"].abs()
    events["reversal_range_frac"] = (events["ret_1"].abs() / events["range_pct"].abs().replace(0, np.nan)).clip(0, 5)
    events["close_location_score"] = np.where(
        events["side"] == "long",
        (1.0 + events["close_to_high"]).clip(0, 1),
        (1.0 - events["close_to_low"]).clip(0, 1),
    )
    events["quality_score"] = quality_score(events)
    events["exit_reason"] = np.where(
        events["gross_return"] >= float(args.target_pct),
        "target_proxy",
        np.where(-events["gross_return"] >= float(args.stop_pct), "stop_proxy", "horizon_proxy"),
    )
    out = events.reset_index()
    out["entry_ts"] = pd.to_datetime(out["timestamp"], utc=True)
    out["exit_ts"] = out["entry_ts"] + pd.to_timedelta(horizon, unit="h")
    out["entry"] = out["close"].astype(float)
    out["exit"] = out["entry"] * (1.0 + out["fwd_ret"].astype(float))
    out["trigger"] = "feature_proxy_reversal"
    keep = [
        "entry_ts",
        "exit_ts",
        "symbol",
        "side",
        "trigger",
        "entry",
        "exit",
        "exit_reason",
        "gross_return",
        "net_return",
        "r_multiple",
        "quality_score",
        *CLUSTER_FEATURES,
        "counter_limit",
        "ret_1",
        "ret_3",
        "ret_6",
        "range_pct",
        "volume_usd",
        "btc_regime_6",
    ]
    out = out[keep].replace([np.inf, -np.inf], np.nan).dropna(subset=["entry_ts", "symbol", "side", "net_return", "quality_score"])
    return out.sort_values(["entry_ts", "quality_score"], ascending=[True, False]).reset_index(drop=True)


def quality_score(events: pd.DataFrame) -> pd.Series:
    trend = (events["h4_trend_abs"].abs() / 0.08).clip(0, 1).fillna(0.0)
    align = (events["h4_trend_align"] / 0.02).clip(0, 1).fillna(0.0)
    pullback = (1.0 - (events["counter_ratio"] - 0.55).abs() / 0.55).clip(0, 1).fillna(0.0)
    reversal = (events["reversal_range_frac"].clip(0, 3) / 1.5).clip(0, 1).fillna(0.0)
    location = events["close_location_score"].clip(0, 1).fillna(0.0)
    vol_ok = (1.0 - (events["atr_14_pct"].fillna(0.015) - 0.015).abs() / 0.035).clip(0, 1)
    return (
        0.24 * trend
        + 0.16 * align
        + 0.20 * pullback
        + 0.18 * reversal
        + 0.12 * location
        + 0.10 * vol_ok
    )


def select_quality_top_frac(candidates: pd.DataFrame, frac: float) -> pd.DataFrame:
    if candidates.empty:
        return candidates.copy()
    frac = min(max(float(frac), 0.01), 1.0)
    rank = candidates.groupby("entry_ts")["quality_score"].rank(method="first", ascending=False)
    count = candidates.groupby("entry_ts")["quality_score"].transform("count")
    return candidates[rank <= np.ceil(count * frac)].copy()


def select_rank_top_n(candidates: pd.DataFrame, n: int) -> pd.DataFrame:
    if candidates.empty:
        return candidates.copy()
    rank = candidates.groupby("entry_ts")["quality_score"].rank(method="first", ascending=False)
    return candidates[rank <= int(n)].copy()


def apply_nonoverlap(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    rows = []
    last_exit_by_symbol: dict[str, pd.Timestamp] = {}
    for row in frame.sort_values(["entry_ts", "quality_score"], ascending=[True, False]).to_dict(orient="records"):
        ts = pd.Timestamp(row["entry_ts"])
        symbol = str(row["symbol"])
        if symbol in last_exit_by_symbol and ts <= last_exit_by_symbol[symbol]:
            continue
        rows.append(row)
        last_exit_by_symbol[symbol] = pd.Timestamp(row["exit_ts"])
    return pd.DataFrame(rows)


def select_rolling_clusters(candidates: pd.DataFrame, args: argparse.Namespace) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if candidates.empty:
        return candidates.copy(), pd.DataFrame(), pd.DataFrame()
    data = candidates.copy()
    data["entry_ts"] = pd.to_datetime(data["entry_ts"], utc=True)
    data = data.sort_values("entry_ts").reset_index(drop=True)
    selected = []
    assignments = []
    stats_rows = []
    model: ClusterModel | None = None
    refit_delta = pd.Timedelta(hours=int(args.cluster_refit_hours))
    train_delta = pd.Timedelta(days=int(args.cluster_train_days))

    for ts, batch in data.groupby("entry_ts", sort=True):
        ts = pd.Timestamp(ts)
        if model is None or ts - model.fitted_at >= refit_delta:
            train = data[(data["entry_ts"] < ts) & (data["entry_ts"] >= ts - train_delta)]
            if len(train) >= int(args.cluster_min_train):
                model = fit_cluster_model(train, ts, args)
                for row in model.stats.to_dict(orient="records"):
                    stats_rows.append({"fit_ts": ts.isoformat(), **row})
        if model is None:
            continue
        labels = predict_clusters(batch, model)
        batch = batch.copy()
        batch["cluster"] = labels
        batch["cluster_selected"] = batch["cluster"].isin(model.eligible_clusters)
        for row in batch[["entry_ts", "symbol", "side", "quality_score", "net_return", "cluster", "cluster_selected"]].to_dict(orient="records"):
            assignments.append(row)
        chosen = batch[batch["cluster_selected"]].copy()
        if not chosen.empty:
            selected.append(chosen)
    trades = pd.concat(selected, ignore_index=True) if selected else data.iloc[0:0].copy()
    diag = pd.DataFrame(assignments)
    stats = pd.DataFrame(stats_rows)
    return trades, diag, stats


def fit_cluster_model(train: pd.DataFrame, fitted_at: pd.Timestamp, args: argparse.Namespace) -> ClusterModel:
    x, mean, scale = feature_matrix(train)
    k = min(int(args.cluster_k), max(2, len(x) // max(1, int(args.cluster_min_count))))
    centers, labels = kmeans(x, k=k, seed=17)
    stats = cluster_performance(train, labels, args)
    eligible = set(
        stats[
            (stats["count"] >= int(args.cluster_min_count))
            & (stats["mean_net_return"] > float(args.cluster_min_mean_return))
            & (stats["win_rate"] >= float(args.cluster_min_win_rate))
        ]["cluster"].astype(int).tolist()
    )
    if not eligible and not stats.empty:
        best = stats.sort_values(["mean_net_return", "win_rate"], ascending=False).head(1)
        if float(best["mean_net_return"].iloc[0]) > 0:
            eligible = {int(best["cluster"].iloc[0])}
    return ClusterModel(fitted_at=fitted_at, mean=mean, scale=scale, centers=centers, eligible_clusters=eligible, stats=stats)


def feature_matrix(frame: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    x = frame[CLUSTER_FEATURES].copy()
    for col in x.columns:
        x[col] = pd.to_numeric(x[col], errors="coerce")
        valid = x[col].dropna()
        med = float(valid.median()) if len(valid) else 0.0
        x[col] = x[col].fillna(med)
    arr = x.to_numpy(dtype=float)
    mean = np.nanmean(arr, axis=0)
    scale = np.nanstd(arr, axis=0)
    scale[scale < 1e-9] = 1.0
    z = np.clip((arr - mean) / scale, -6, 6)
    return z, mean, scale


def kmeans(x: np.ndarray, k: int, seed: int = 17, max_iter: int = 40) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    if len(x) <= k:
        centers = x.copy()
        labels = np.arange(len(x))
        return centers, labels
    first = int(rng.integers(0, len(x)))
    centers = [x[first]]
    dist = np.sum((x - centers[0]) ** 2, axis=1)
    for _ in range(1, k):
        probs = dist / max(float(dist.sum()), 1e-12)
        idx = int(rng.choice(len(x), p=probs))
        centers.append(x[idx])
        dist = np.minimum(dist, np.sum((x - centers[-1]) ** 2, axis=1))
    centers_arr = np.vstack(centers)
    labels = np.zeros(len(x), dtype=int)
    for _ in range(max_iter):
        d = ((x[:, None, :] - centers_arr[None, :, :]) ** 2).sum(axis=2)
        new_labels = d.argmin(axis=1)
        if np.array_equal(new_labels, labels):
            break
        labels = new_labels
        for j in range(k):
            if np.any(labels == j):
                centers_arr[j] = x[labels == j].mean(axis=0)
    return centers_arr, labels


def predict_clusters(frame: pd.DataFrame, model: ClusterModel) -> np.ndarray:
    x = frame[CLUSTER_FEATURES].copy()
    for idx, col in enumerate(x.columns):
        x[col] = pd.to_numeric(x[col], errors="coerce").fillna(float(model.mean[idx]))
    arr = np.clip((x.to_numpy(dtype=float) - model.mean) / model.scale, -6, 6)
    d = ((arr[:, None, :] - model.centers[None, :, :]) ** 2).sum(axis=2)
    return d.argmin(axis=1).astype(int)


def cluster_performance(train: pd.DataFrame, labels: np.ndarray, args: argparse.Namespace) -> pd.DataFrame:
    rows = []
    tmp = train.copy()
    tmp["cluster"] = labels
    for cluster, group in tmp.groupby("cluster"):
        ret = pd.to_numeric(group["net_return"], errors="coerce").dropna()
        if ret.empty:
            continue
        row: dict[str, Any] = {
            "cluster": int(cluster),
            "count": int(len(ret)),
            "win_rate": float((ret > 0).mean()),
            "mean_net_return": float(ret.mean()),
            "median_net_return": float(ret.median()),
            "sum_net_return": float(ret.sum()),
            "eligible": bool(
                len(ret) >= int(args.cluster_min_count)
                and float(ret.mean()) > float(args.cluster_min_mean_return)
                and float((ret > 0).mean()) >= float(args.cluster_min_win_rate)
            ),
        }
        for feature in CLUSTER_FEATURES:
            row[f"{feature}_mean"] = float(pd.to_numeric(group[feature], errors="coerce").mean())
        rows.append(row)
    return pd.DataFrame(rows)


def summarize(frame: pd.DataFrame, args: argparse.Namespace) -> dict[str, Any]:
    if frame.empty:
        return {
            "trades": 0,
            "symbols": 0,
            "win_rate": math.nan,
            "mean_net_return": math.nan,
            "median_net_return": math.nan,
            "total_net_return_units": 0.0,
            "positive_month_rate": math.nan,
            "score": -1e9,
            "verdict": "NO_SIGNAL",
        }
    ret = pd.to_numeric(frame["net_return"], errors="coerce").dropna()
    wins = ret > 0
    month = frame.copy()
    month["month"] = pd.to_datetime(month["entry_ts"], utc=True).dt.strftime("%Y-%m")
    monthly = month.groupby("month")["net_return"].sum()
    mean = float(ret.mean())
    std = float(ret.std()) if len(ret) > 1 else math.nan
    sharpe_like = float(mean / std * math.sqrt(365 * 24 / max(1, int(args.max_hold_hours)))) if std and std > 0 else math.nan
    positive_month_rate = float((monthly > 0).mean()) if len(monthly) else math.nan
    worst_month = float(monthly.min()) if len(monthly) else math.nan
    score = mean * 100.0 + (float(wins.mean()) - 0.5) * 2.0 + (positive_month_rate - 0.5) * 1.5 - abs(min(0.0, worst_month)) * 4.0
    verdict = "ROBUST" if float(wins.mean()) >= 0.54 and mean > 0 and positive_month_rate >= 0.58 else "MARGINAL" if mean > 0 and float(wins.mean()) >= 0.50 else "RANDOM"
    return {
        "trades": int(len(ret)),
        "symbols": int(frame["symbol"].nunique()),
        "win_rate": float(wins.mean()),
        "mean_net_return": mean,
        "median_net_return": float(ret.median()),
        "total_net_return_units": float(ret.sum()),
        "sharpe_like": sharpe_like,
        "positive_month_rate": positive_month_rate,
        "worst_month_units": worst_month,
        "target_rate": float(frame["exit_reason"].astype(str).str.startswith("target").mean()),
        "stop_rate": float(frame["exit_reason"].astype(str).str.startswith("stop").mean()),
        "horizon_rate": float(frame["exit_reason"].astype(str).str.startswith("horizon").mean()),
        "long_trades": int((frame["side"] == "long").sum()),
        "short_trades": int((frame["side"] == "short").sum()),
        "score": float(score),
        "verdict": verdict,
    }


def parse_top_ns(value: str) -> list[int]:
    out = []
    for item in str(value).split(","):
        item = item.strip()
        if not item:
            continue
        out.append(max(1, int(item)))
    return sorted(set(out))


def experiment_plan(args: argparse.Namespace) -> str:
    return f"""# Trend Pullback Reversal Variant Experiment Plan

Generated: {datetime.now(timezone.utc).isoformat()}

Shape definition:
- 4h trend direction from `h4_ret_6` and `h4_ret_1`.
- 1h countertrend pullback from `ret_3`.
- 1h reversal trigger from `ret_1`, range, and close location near the bar extreme.
- Same raw candidate table is shared by every variant.

Variants:
1. `baseline`: enter every raw candidate after same-symbol non-overlap.
2. `quality_top{int(args.quality_top_frac * 100)}`: hand-scored quality top {args.quality_top_frac:.0%} per scan timestamp.
3. `rank_topN`: hand-scored market ranking, only top N per scan timestamp for N={args.rank_top_n}.
4. `rolling_cluster`: fit scaler + k-means only on prior candidate events, every {args.cluster_refit_hours}h, over a {args.cluster_train_days}d trailing window. Current candidates are traded only if their predicted cluster had prior mean return > {args.cluster_min_mean_return:g}, win rate >= {args.cluster_min_win_rate:.2%}, and at least {args.cluster_min_count} prior samples.

Leakage controls:
- Candidate features are known at entry time.
- Future return is used only for evaluation and trailing cluster performance after the event is in the past.
- Cluster scaler and centers are fitted only with `entry_ts < current_ts`.
- Cluster eligibility is computed only from the same trailing historical training window.

Dataset:
- `{args.dataset_id}`
- start `{args.start}`
- end `{args.end or 'now'}`
- max_symbols `{args.max_symbols}`
"""


def markdown_report(payload: dict[str, Any], comparison: pd.DataFrame, cluster_stats: pd.DataFrame) -> str:
    lines = [
        "# Trend Pullback Reversal Variants",
        "",
        f"Generated: {payload['generated_at']}",
        f"Candidate events: {payload['candidate_events']}",
        "",
        "## Comparison",
        "",
        "| Variant | Trades | Win | Mean | Total Units | Pos Months | Worst Month | Verdict |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in comparison.to_dict(orient="records"):
        lines.append(
            "| {variant} | {trades} | {win_rate:.2%} | {mean_net_return:.3%} | {total_net_return_units:.2f} | {positive_month_rate:.2%} | {worst_month_units:.2f} | {verdict} |".format(
                **row
            )
        )
    if not cluster_stats.empty:
        latest_ts = cluster_stats["fit_ts"].max()
        latest = cluster_stats[cluster_stats["fit_ts"] == latest_ts].sort_values("mean_net_return", ascending=False)
        lines.extend(["", "## Latest Cluster Snapshot", "", f"Fit timestamp: {latest_ts}", ""])
        lines.append("| Cluster | Count | Win | Mean | Eligible |")
        lines.append("|---:|---:|---:|---:|---|")
        for row in latest.to_dict(orient="records"):
            lines.append("| {cluster} | {count} | {win_rate:.2%} | {mean_net_return:.3%} | {eligible} |".format(**row))
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
