#!/usr/bin/env python3
"""Run a bounded parameter sweep for monster lottery backtests."""

from __future__ import annotations

import argparse
import itertools
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OUT_ROOT = ROOT / "engine" / "data" / "monster_events"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sweep monster lottery backtest parameters")
    p.add_argument("--sweep-id", default=None)
    p.add_argument("--start", default="2026-01-01")
    p.add_argument("--end", default="2026-04-26")
    p.add_argument("--rebalance-minutes", default="240")
    p.add_argument("--risk-budgets", default="10,20")
    p.add_argument("--long-scores", default="0.88,0.90,0.92,0.95")
    p.add_argument("--stop-losses", default="0.08,0.10,0.15")
    p.add_argument("--tp-packs", default="0.30:0.80:0.25,0.50:1.50:0.30")
    p.add_argument("--max-runs", type=int, default=0, help="0 means no cap")
    p.add_argument("--progress", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    sweep_id = args.sweep_id or f"monster_lottery_sweep_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    out_dir = OUT_ROOT / sweep_id
    out_dir.mkdir(parents=True, exist_ok=True)
    progress_path = out_dir / "progress.jsonl"

    configs = _configs(args)
    if args.max_runs:
        configs = configs[: args.max_runs]
    rows = []
    for i, cfg in enumerate(configs, start=1):
        run_id = f"{sweep_id}_run_{i:03d}"
        cmd = [
            sys.executable,
            "scripts/backtest_monster_lottery.py",
            "--dataset-id",
            run_id,
            "--start",
            args.start,
            "--end",
            args.end,
            "--rebalance-minutes",
            str(args.rebalance_minutes),
            "--risk-budget",
            str(cfg["risk_budget"]),
            "--long-score",
            str(cfg["long_score"]),
            "--stop-loss",
            str(cfg["stop_loss"]),
            "--tp1",
            str(cfg["tp1"]),
            "--tp2",
            str(cfg["tp2"]),
            "--runner-trailing",
            str(cfg["runner_trailing"]),
            "--progress-every",
            "0",
        ]
        record = {"run_index": i, "run_id": run_id, **cfg, "status": "running", "cmd": cmd}
        _append_jsonl(progress_path, record)
        if args.progress:
            print(f"[{i}/{len(configs)}] {run_id} {cfg}", flush=True)
        result = subprocess.run(cmd, cwd=str(ROOT), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        record["returncode"] = result.returncode
        record["status"] = "ok" if result.returncode == 0 else "failed"
        record["output_tail"] = result.stdout[-2000:]
        metrics_path = OUT_ROOT / run_id / "metrics.json"
        if metrics_path.exists():
            metrics = json.loads(metrics_path.read_text())
            record.update({f"metric_{k}": v for k, v in metrics.items()})
            record["score_objective"] = _objective(metrics)
        rows.append(record)
        _append_jsonl(progress_path, record)

    df = pd.DataFrame(rows)
    df.to_csv(out_dir / "results.csv", index=False)
    if not df.empty:
        df.to_parquet(out_dir / "results.parquet")
    ranked = df.sort_values("score_objective", ascending=False, na_position="last") if "score_objective" in df else df
    top = ranked.head(20).to_dict(orient="records")
    manifest = {
        "sweep_id": sweep_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "start": args.start,
        "end": args.end,
        "runs": len(configs),
        "artifacts": {
            "results_csv": str((out_dir / "results.csv").relative_to(ROOT)),
            "progress_jsonl": str(progress_path.relative_to(ROOT)),
        },
        "top": top,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str))
    print(json.dumps(manifest, indent=2, sort_keys=True, default=str))
    return 0


def _configs(args: argparse.Namespace) -> list[dict[str, Any]]:
    risk_budgets = [float(x) for x in args.risk_budgets.split(",") if x]
    long_scores = [float(x) for x in args.long_scores.split(",") if x]
    stop_losses = [float(x) for x in args.stop_losses.split(",") if x]
    tp_packs = []
    for raw in args.tp_packs.split(","):
        if not raw:
            continue
        tp1, tp2, trail = [float(x) for x in raw.split(":")]
        tp_packs.append({"tp1": tp1, "tp2": tp2, "runner_trailing": trail})
    configs = []
    for risk, score, stop, tp in itertools.product(risk_budgets, long_scores, stop_losses, tp_packs):
        configs.append({"risk_budget": risk, "long_score": score, "stop_loss": stop, **tp})
    return configs


def _objective(metrics: dict[str, Any]) -> float:
    total_return = float(metrics.get("total_return") or 0.0)
    max_dd = abs(float(metrics.get("max_drawdown") or 0.0))
    best_r = float(metrics.get("best_return_on_risk_budget") or 0.0)
    worst_r = abs(float(metrics.get("worst_return_on_risk_budget") or 0.0))
    profit_factor = float(metrics.get("profit_factor") or 0.0)
    return total_return * 2.0 + best_r * 0.15 + profit_factor * 0.25 - max_dd * 1.5 - worst_r * 0.15


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    with path.open("a") as fh:
        fh.write(json.dumps(payload, sort_keys=True, default=str) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
