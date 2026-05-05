#!/usr/bin/env python3
"""Run C-Auto ML research experiments from materialized feature datasets."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

import numpy as np
import pandas as pd

ENGINE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE_DIR))

from config.settings import BASE_DIR
from registry import PerformanceRecord, StrategyRegistry

try:
    from sklearn.linear_model import Ridge
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
except Exception as exc:  # pragma: no cover - dependency check happens at runtime
    Ridge = None
    StandardScaler = None
    make_pipeline = None
    SKLEARN_IMPORT_ERROR = exc
else:
    SKLEARN_IMPORT_ERROR = None


DEFAULT_STRATEGY_ID = "core_c_auto_h24_regression_v1"
DEFAULT_PARAMETER_SET_ID = "core_c_auto_h24_regression_v1.default"
DEFAULT_LABEL_COL = "fwd_ret_net_long_24"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Evaluate c-auto parameter sets on a research dataset")
    p.add_argument("--dataset-id", required=True, help="Directory name under engine/data/features")
    p.add_argument("--strategy-id", default=DEFAULT_STRATEGY_ID)
    p.add_argument("--parameter-set-id", default=DEFAULT_PARAMETER_SET_ID)
    p.add_argument("--label-col", default=DEFAULT_LABEL_COL)
    p.add_argument("--out-id", default=None)
    p.add_argument("--register-performance", action="store_true")
    p.add_argument("--notes", default="")
    p.add_argument("--max-folds", type=int, default=0, help="Optional cap for quick smoke runs")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    registry = StrategyRegistry()
    params = dict(registry.get_parameter_set(args.parameter_set_id).params)

    dataset_dir = BASE_DIR / "data" / "features" / args.dataset_id
    out_id = args.out_id or f"{args.strategy_id}_{args.parameter_set_id}_{_stamp()}".replace("/", "_")
    out_dir = BASE_DIR / "data" / "research" / "c_auto" / out_id
    out_dir.mkdir(parents=True, exist_ok=True)

    features = _read_frame(dataset_dir, "features")
    labels = _read_frame(dataset_dir, "labels")
    if features.empty or labels.empty:
        raise RuntimeError(f"Dataset has empty features or labels: {dataset_dir}")
    if args.label_col not in labels.columns:
        raise KeyError(f"Label column {args.label_col!r} not found in {dataset_dir}")

    feature_cols = [col for col in params.get("feature_columns", []) if col in features.columns]
    if not feature_cols:
        raise RuntimeError("No configured c-auto feature columns exist in the dataset")

    panel = features[feature_cols].join(labels[[args.label_col]], how="inner")
    folds = _load_folds(dataset_dir)
    if args.max_folds and args.max_folds > 0:
        folds = folds[: args.max_folds]
    if not folds:
        folds = [_single_holdout_fold(panel)]

    predictions = _run_folds(
        panel=panel,
        folds=folds,
        feature_cols=feature_cols,
        label_col=args.label_col,
        ridge_alpha=float(params.get("ridge_alpha", 10.0)),
    )
    metrics = _metrics(
        predictions,
        label_col=args.label_col,
        long_quantile=float(params.get("long_quantile", 0.8)),
        short_quantile=float(params.get("short_quantile", 0.2)),
        min_abs_prediction=float(params.get("min_abs_prediction", 0.0)),
    )
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "experiment_id": out_id,
        "strategy_id": args.strategy_id,
        "parameter_set_id": args.parameter_set_id,
        "dataset_id": args.dataset_id,
        "dataset_dir": str(dataset_dir),
        "label_col": args.label_col,
        "feature_columns": feature_cols,
        "fold_count": len(folds),
        "model_backend": "sklearn_ridge" if Ridge is not None and StandardScaler is not None and make_pipeline is not None else "fallback_linear_score",
        "sklearn_import_error": str(SKLEARN_IMPORT_ERROR) if SKLEARN_IMPORT_ERROR else None,
        "params": params,
        "metrics": metrics,
        "notes": args.notes,
    }

    _write_frame(predictions, out_dir / "predictions.parquet")
    (out_dir / "metrics.json").write_text(json.dumps(metrics, indent=2, sort_keys=True))
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))

    if args.register_performance:
        record = PerformanceRecord(
            record_id=f"perf-{uuid4()}",
            strategy_id=args.strategy_id,
            parameter_set_id=args.parameter_set_id,
            mode="backtest",
            start=str(metrics.get("start", "")),
            end=str(metrics.get("end", "")),
            metrics=metrics,
            costs=_costs_from_dataset(dataset_dir),
            dataset_id=args.dataset_id,
            decision_journal_path=str(out_dir),
            notes=args.notes or f"c-auto research experiment {out_id}",
        )
        registry.add_performance(record)
        manifest["performance_record_id"] = record.record_id
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


def _load_folds(dataset_dir: Path) -> list[dict[str, Any]]:
    path = dataset_dir / "walk_forward_folds.json"
    if not path.exists():
        return []
    return json.loads(path.read_text())


def _single_holdout_fold(panel: pd.DataFrame) -> dict[str, Any]:
    ts = _timestamps(panel)
    split = max(1, int(len(ts) * 0.70))
    return {
        "fold": 0,
        "train_start": str(ts[0]),
        "train_end": str(ts[split - 1]),
        "test_start": str(ts[split]),
        "test_end": str(ts[-1]),
        "purge_bars": 0,
    }


def _run_folds(
    panel: pd.DataFrame,
    folds: list[Mapping[str, Any]],
    feature_cols: list[str],
    label_col: str,
    ridge_alpha: float,
) -> pd.DataFrame:
    rows: list[pd.DataFrame] = []
    for fold in folds:
        train = _slice_panel(panel, str(fold["train_start"]), str(fold["train_end"]))
        test = _slice_panel(panel, str(fold["test_start"]), str(fold["test_end"]))
        train = train.dropna(subset=[label_col])
        if len(train) < 50 or test.empty:
            continue

        x_train = train[feature_cols].apply(pd.to_numeric, errors="coerce")
        y_train = pd.to_numeric(train[label_col], errors="coerce")
        keep = y_train.notna()
        x_train = x_train.loc[keep]
        y_train = y_train.loc[keep]
        medians = x_train.median(numeric_only=True).replace([np.inf, -np.inf], np.nan).fillna(0.0)
        x_train = x_train.replace([np.inf, -np.inf], np.nan).fillna(medians)

        x_test = test[feature_cols].apply(pd.to_numeric, errors="coerce")
        x_test = x_test.replace([np.inf, -np.inf], np.nan).fillna(medians).fillna(0.0)
        pred = test[[label_col]].copy()
        if Ridge is not None and StandardScaler is not None and make_pipeline is not None:
            model = make_pipeline(StandardScaler(), Ridge(alpha=ridge_alpha))
            model.fit(x_train.to_numpy(dtype=float), y_train.to_numpy(dtype=float))
            pred["prediction"] = model.predict(x_test.to_numpy(dtype=float))
        else:
            corr = x_train.corrwith(y_train).replace([np.inf, -np.inf], np.nan).fillna(0.0)
            score = (x_test - x_train.mean()).divide(x_train.std().replace(0, np.nan)).fillna(0.0)
            fallback_pred = score.mul(corr, axis=1).sum(axis=1).to_numpy(dtype=float)
            fallback_pred = fallback_pred / max(1.0, float(len(feature_cols))) * max(float(y_train.std() or 0.0), 0.001)
            pred["prediction"] = fallback_pred
        pred["fold"] = int(fold.get("fold", len(rows)))
        pred["rank_pct"] = pred.groupby(level="timestamp")["prediction"].rank(pct=True, method="average")
        rows.append(pred)
    if not rows:
        return pd.DataFrame(columns=[label_col, "prediction", "fold", "rank_pct"])
    return pd.concat(rows).sort_index()


def _metrics(
    predictions: pd.DataFrame,
    label_col: str,
    long_quantile: float,
    short_quantile: float,
    min_abs_prediction: float,
) -> dict[str, Any]:
    if predictions.empty:
        return {"rows": 0, "status": "empty"}

    df = predictions.dropna(subset=[label_col, "prediction"]).copy()
    df["selected"] = (
        ((df["rank_pct"] >= long_quantile) | (df["rank_pct"] <= short_quantile))
        & (df["prediction"].abs() >= min_abs_prediction)
    )
    selected = df[df["selected"]]
    long_tail = df[df["rank_pct"] >= long_quantile]
    short_tail = df[df["rank_pct"] <= short_quantile]
    by_fold = []
    for fold, group in df.groupby("fold"):
        by_fold.append(_fold_metrics(int(fold), group, label_col, long_quantile, short_quantile))

    return {
        "status": "ok",
        "start": str(_timestamps(df)[0]),
        "end": str(_timestamps(df)[-1]),
        "rows": int(len(df)),
        "folds": int(df["fold"].nunique()),
        "selected_rows": int(len(selected)),
        "selection_rate": float(len(selected) / len(df)) if len(df) else 0.0,
        "spearman_ic": _safe_corr(df["prediction"], df[label_col], method="spearman"),
        "pearson_ic": _safe_corr(df["prediction"], df[label_col], method="pearson"),
        "directional_accuracy": float((np.sign(df["prediction"]) == np.sign(df[label_col])).mean()),
        "selected_mean_return": _safe_mean(selected[label_col]),
        "long_tail_mean_return": _safe_mean(long_tail[label_col]),
        "short_tail_mean_return": _safe_mean(short_tail[label_col]),
        "long_short_spread": _safe_mean(long_tail[label_col]) - _safe_mean(short_tail[label_col]),
        "prediction_mean": _safe_mean(df["prediction"]),
        "prediction_std": _safe_std(df["prediction"]),
        "label_mean": _safe_mean(df[label_col]),
        "label_std": _safe_std(df[label_col]),
        "fold_metrics": by_fold,
    }


def _fold_metrics(
    fold: int,
    group: pd.DataFrame,
    label_col: str,
    long_quantile: float,
    short_quantile: float,
) -> dict[str, Any]:
    long_tail = group[group["rank_pct"] >= long_quantile]
    short_tail = group[group["rank_pct"] <= short_quantile]
    return {
        "fold": fold,
        "rows": int(len(group)),
        "spearman_ic": _safe_corr(group["prediction"], group[label_col], method="spearman"),
        "directional_accuracy": float((np.sign(group["prediction"]) == np.sign(group[label_col])).mean()),
        "long_short_spread": _safe_mean(long_tail[label_col]) - _safe_mean(short_tail[label_col]),
    }


def _slice_panel(panel: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    ts = pd.to_datetime(panel.index.get_level_values("timestamp"), utc=True)
    mask = (ts >= pd.Timestamp(start)) & (ts <= pd.Timestamp(end))
    return panel.loc[mask]


def _timestamps(df: pd.DataFrame) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(pd.to_datetime(df.index.get_level_values("timestamp"), utc=True).unique()).sort_values()


def _safe_corr(a: pd.Series, b: pd.Series, method: str) -> float:
    left = pd.to_numeric(a, errors="coerce")
    right = pd.to_numeric(b, errors="coerce")
    if method == "spearman":
        left = left.rank(method="average")
        right = right.rank(method="average")
        method = "pearson"
    value = left.corr(right, method=method)
    return 0.0 if value is None or not np.isfinite(value) else float(value)


def _safe_mean(series: pd.Series) -> float:
    value = pd.to_numeric(series, errors="coerce").mean()
    return 0.0 if not np.isfinite(value) else float(value)


def _safe_std(series: pd.Series) -> float:
    value = pd.to_numeric(series, errors="coerce").std()
    return 0.0 if not np.isfinite(value) else float(value)


def _costs_from_dataset(dataset_dir: Path) -> dict[str, Any]:
    meta = dataset_dir / "metadata.json"
    if not meta.exists():
        return {}
    try:
        return dict(json.loads(meta.read_text()).get("cost_assumptions", {}))
    except Exception:
        return {}


def _write_frame(df: pd.DataFrame, path: Path) -> None:
    try:
        df.to_parquet(path)
    except Exception:
        df.to_pickle(path.with_suffix(".pkl"))


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


if __name__ == "__main__":
    raise SystemExit(main())
