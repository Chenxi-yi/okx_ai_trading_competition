#!/usr/bin/env python3
"""Evaluate monster candidate scoring rules on labeled research samples."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from build_monster_dataset import OUT_ROOT, _relpath  # noqa: E402
from score_monster_watchlist import _score_row, _select_score_features  # noqa: E402

DEFAULT_SAMPLES = OUT_ROOT / "monster_samples_clustered_5m_v1" / "samples.parquet"
DEFAULT_FEATURE_SUMMARY = OUT_ROOT / "monster_samples_clustered_5m_v1" / "feature_summary.csv"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Evaluate monster candidate rules on research samples")
    p.add_argument("--samples", default=str(DEFAULT_SAMPLES))
    p.add_argument("--feature-summary", default=str(DEFAULT_FEATURE_SUMMARY))
    p.add_argument("--dataset-id", default="monster_rule_eval_clustered_v1")
    p.add_argument("--feature-count", type=int, default=25)
    p.add_argument("--fee-bps", type=float, default=8.0, help="Round-trip fee+slippage cost in bps")
    p.add_argument("--thresholds", default="0.60,0.65,0.70,0.75,0.80,0.85,0.90")
    p.add_argument("--max-ret-1h", type=float, default=0.25)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    samples = pd.read_parquet(args.samples)
    feature_summary = pd.read_csv(args.feature_summary)
    score_features = _select_score_features(feature_summary, samples, args.feature_count)
    scored = _score_samples(samples, score_features, args.max_ret_1h)
    thresholds = [float(x) for x in args.thresholds.split(",") if x.strip()]
    summary = _threshold_summary(scored, thresholds, args.fee_bps)

    out_dir = OUT_ROOT / args.dataset_id
    out_dir.mkdir(parents=True, exist_ok=True)
    scored_path = out_dir / "scored_samples.parquet"
    summary_path = out_dir / "threshold_summary.csv"
    report_path = out_dir / "report.md"
    scored.to_parquet(scored_path)
    summary.to_csv(summary_path, index=False)
    report_path.write_text(_markdown_report(summary, score_features))
    manifest = {
        "dataset_id": args.dataset_id,
        "created_at": pd.Timestamp.utcnow().isoformat(),
        "samples": _relpath(Path(args.samples)),
        "feature_summary": _relpath(Path(args.feature_summary)),
        "rows": int(len(scored)),
        "feature_count": len(score_features),
        "fee_bps": args.fee_bps,
        "max_ret_1h": args.max_ret_1h,
        "artifacts": {
            "scored_samples": _relpath(scored_path),
            "threshold_summary": _relpath(summary_path),
            "report": _relpath(report_path),
        },
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str))
    print(json.dumps(manifest, indent=2, sort_keys=True, default=str))
    print(summary.to_string(index=False))
    return 0


def _score_samples(samples: pd.DataFrame, score_features: list[dict[str, Any]], max_ret_1h: float) -> pd.DataFrame:
    rows = []
    for row in samples.to_dict(orient="records"):
        score = _score_row(row, samples, score_features)
        row.update(score)
        row["rule_market_ok"] = int(row.get("market_event_flag", 0) == 0)
        row["rule_not_vertical_1h"] = int(pd.isna(row.get("ret_1h")) or row.get("ret_1h") <= max_ret_1h)
        rows.append(row)
    return pd.DataFrame(rows)


def _threshold_summary(scored: pd.DataFrame, thresholds: list[float], fee_bps: float) -> pd.DataFrame:
    cost = fee_bps / 10000.0
    rows = []
    base = scored[(scored["rule_market_ok"] == 1) & (scored["rule_not_vertical_1h"] == 1)].copy()
    for threshold in thresholds:
        subset = base[base["monster_score_adj"] >= threshold].copy()
        rows.append(_summarize_subset(subset, threshold, cost))
    return pd.DataFrame(rows)


def _summarize_subset(df: pd.DataFrame, threshold: float, cost: float) -> dict[str, Any]:
    if df.empty:
        return {"score_threshold": threshold, "rows": 0}
    fwd_1d = pd.to_numeric(df["fwd_ret_1d"], errors="coerce") - cost
    fwd_3d = pd.to_numeric(df["fwd_ret_3d"], errors="coerce") - cost
    fwd_5d = pd.to_numeric(df["fwd_ret_5d"], errors="coerce") - cost
    max_5d = pd.to_numeric(df["max_fwd_ret_5d"], errors="coerce") - cost
    min_5d = pd.to_numeric(df["min_fwd_ret_5d"], errors="coerce") - cost
    return {
        "score_threshold": threshold,
        "rows": int(len(df)),
        "positive_rate": float(df["label_monster"].mean()),
        "symbols": int(df["symbol"].nunique()),
        "fwd_1d_median": float(fwd_1d.median()),
        "fwd_1d_mean": float(fwd_1d.mean()),
        "fwd_1d_win_rate": float((fwd_1d > 0).mean()),
        "fwd_3d_median": float(fwd_3d.median()),
        "fwd_3d_mean": float(fwd_3d.mean()),
        "fwd_3d_win_rate": float((fwd_3d > 0).mean()),
        "fwd_5d_median": float(fwd_5d.median()),
        "fwd_5d_mean": float(fwd_5d.mean()),
        "fwd_5d_win_rate": float((fwd_5d > 0).mean()),
        "max_5d_median": float(max_5d.median()),
        "min_5d_median": float(min_5d.median()),
    }


def _markdown_report(summary: pd.DataFrame, score_features: list[dict[str, Any]]) -> str:
    lines = [
        "# Monster Candidate Rule Evaluation",
        "",
        "## Threshold Summary",
        "",
        summary.to_markdown(index=False),
        "",
        "## Score Features",
        "",
        pd.DataFrame(score_features).to_markdown(index=False),
        "",
        "## Notes",
        "",
        "- This is sample-set calibration, not a full chronological portfolio backtest.",
        "- It evaluates the same shape score used by the live watchlist on positive/negative research samples with known forward returns.",
    ]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
