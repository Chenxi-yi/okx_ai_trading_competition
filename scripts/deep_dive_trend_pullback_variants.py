#!/usr/bin/env python3
"""Deep-dive selected trend-pullback reversal variants.

Reads a previously generated candidate table and evaluates:
  - quality top-X fractions
  - rank top-N market competition
  - stricter rolling-cluster gates
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SOURCE_SCRIPT = ROOT / "scripts" / "run_trend_pullback_reversal_variants.py"
OUT_ROOT = ROOT / "engine" / "data" / "research" / "trend_pullback_reversal"


def load_core() -> Any:
    spec = importlib.util.spec_from_file_location("trend_pullback_core", SOURCE_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {SOURCE_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[str(spec.name)] = module
    spec.loader.exec_module(module)
    return module


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Deep-dive trend-pullback selected variants")
    p.add_argument("--source-dir", default="engine/data/research/trend_pullback_reversal/trend_pullback_variants_4way_20260517")
    p.add_argument("--out-id", default="")
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
    out_id = args.out_id or f"trend_pullback_deep_dive_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    out_dir = OUT_ROOT / out_id
    out_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    artifacts: dict[str, str] = {}

    quality_rows = []
    for frac in (0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50, 0.60):
        trades = core.apply_nonoverlap(core.select_quality_top_frac(candidates, frac))
        name = f"quality_top{int(frac * 100):02d}"
        trades.to_csv(out_dir / f"{name}_trades.csv", index=False)
        row = {"family": "quality", "variant": name, "param": frac, **core.summarize(trades, base_args)}
        rows.append(row)
        quality_rows.append(row)

    rank_rows = []
    for n in range(1, 11):
        trades = core.apply_nonoverlap(core.select_rank_top_n(candidates, n))
        name = f"rank_top{n}"
        trades.to_csv(out_dir / f"{name}_trades.csv", index=False)
        row = {"family": "rank", "variant": name, "param": n, **core.summarize(trades, base_args)}
        rows.append(row)
        rank_rows.append(row)

    cluster_configs = [
        {"name": "cluster_default", "cluster_k": 6, "cluster_train_days": 180, "cluster_min_count": 40, "cluster_min_win_rate": 0.52, "cluster_min_mean_return": 0.0},
        {"name": "cluster_strict55_mean30bp", "cluster_k": 6, "cluster_train_days": 180, "cluster_min_count": 60, "cluster_min_win_rate": 0.55, "cluster_min_mean_return": 0.003},
        {"name": "cluster_strict58_mean50bp", "cluster_k": 6, "cluster_train_days": 180, "cluster_min_count": 60, "cluster_min_win_rate": 0.58, "cluster_min_mean_return": 0.005},
        {"name": "cluster_elite60_mean100bp", "cluster_k": 6, "cluster_train_days": 180, "cluster_min_count": 80, "cluster_min_win_rate": 0.60, "cluster_min_mean_return": 0.010},
        {"name": "cluster_k8_strict55", "cluster_k": 8, "cluster_train_days": 180, "cluster_min_count": 60, "cluster_min_win_rate": 0.55, "cluster_min_mean_return": 0.003},
        {"name": "cluster_k10_strict55", "cluster_k": 10, "cluster_train_days": 180, "cluster_min_count": 60, "cluster_min_win_rate": 0.55, "cluster_min_mean_return": 0.003},
        {"name": "cluster_train90_strict55", "cluster_k": 6, "cluster_train_days": 90, "cluster_min_count": 40, "cluster_min_win_rate": 0.55, "cluster_min_mean_return": 0.003},
        {"name": "cluster_train270_strict55", "cluster_k": 6, "cluster_train_days": 270, "cluster_min_count": 80, "cluster_min_win_rate": 0.55, "cluster_min_mean_return": 0.003},
    ]
    cluster_rows = []
    for cfg in cluster_configs:
        cluster_args = argparse.Namespace(**vars(base_args))
        for key, value in cfg.items():
            if key != "name":
                setattr(cluster_args, key, value)
        trades, diag, stats = core.select_rolling_clusters(candidates, cluster_args)
        trades = core.apply_nonoverlap(trades)
        name = cfg["name"]
        trades.to_csv(out_dir / f"{name}_trades.csv", index=False)
        diag.to_csv(out_dir / f"{name}_assignments.csv", index=False)
        stats.to_csv(out_dir / f"{name}_refit_stats.csv", index=False)
        artifacts[f"{name}_refit_stats"] = f"{name}_refit_stats.csv"
        row = {"family": "cluster", "variant": name, "param": json.dumps({k: v for k, v in cfg.items() if k != "name"}, sort_keys=True), **core.summarize(trades, cluster_args)}
        rows.append(row)
        cluster_rows.append(row)

    comparison = pd.DataFrame(rows).sort_values(["score", "mean_net_return"], ascending=False)
    comparison.to_csv(out_dir / "deep_dive_comparison.csv", index=False)
    pd.DataFrame(quality_rows).sort_values(["score", "mean_net_return"], ascending=False).to_csv(out_dir / "quality_sweep.csv", index=False)
    pd.DataFrame(rank_rows).sort_values(["score", "mean_net_return"], ascending=False).to_csv(out_dir / "rank_sweep.csv", index=False)
    pd.DataFrame(cluster_rows).sort_values(["score", "mean_net_return"], ascending=False).to_csv(out_dir / "cluster_sweep.csv", index=False)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_dir": str(source_dir.relative_to(ROOT)),
        "candidate_events": int(len(candidates)),
        "comparison": comparison.to_dict(orient="records"),
        "artifacts": {
            "deep_dive_comparison": "deep_dive_comparison.csv",
            "quality_sweep": "quality_sweep.csv",
            "rank_sweep": "rank_sweep.csv",
            "cluster_sweep": "cluster_sweep.csv",
            **artifacts,
        },
    }
    (out_dir / "summary.json").write_text(json.dumps(payload, indent=2, sort_keys=True))
    (out_dir / "report.md").write_text(markdown_report(payload, comparison))
    print(json.dumps({"out_dir": str(out_dir.relative_to(ROOT)), **payload}, indent=2, sort_keys=True))
    return 0


def markdown_report(payload: dict[str, Any], comparison: pd.DataFrame) -> str:
    lines = [
        "# Trend Pullback Reversal Deep Dive",
        "",
        f"Generated: {payload['generated_at']}",
        f"Source: `{payload['source_dir']}`",
        f"Candidate events: {payload['candidate_events']}",
        "",
        "## Top Results",
        "",
        "| Family | Variant | Trades | Win | Mean | Total Units | Pos Months | Worst Month |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in comparison.head(20).to_dict(orient="records"):
        lines.append(
            "| {family} | {variant} | {trades} | {win_rate:.2%} | {mean_net_return:.3%} | {total_net_return_units:.2f} | {positive_month_rate:.2%} | {worst_month_units:.2f} |".format(
                **row
            )
        )
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
