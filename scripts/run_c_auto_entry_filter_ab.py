#!/usr/bin/env python3
"""Run focused C-Auto entry-filter A/B backtests."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUT_ROOT = ROOT / "engine" / "data" / "research" / "c_auto_entry_filter_ab"


BASE_ARGS = [
    "--dataset-id",
    "c_auto_feature_store_rebuild_161_ohlcv_snapshot_v1",
    "--initial-capital",
    "3000",
    "--sizing-mode",
    "fixed",
    "--fixed-notional-capital",
    "3000",
    "--max-positions",
    "5",
    "--rebalance-hours",
    "6",
    "--entry-delay-hours",
    "1",
    "--fee-bps-per-side",
    "5",
    "--slippage-bps-per-side",
    "2",
    "--base-risk",
    "0.18",
    "--min-score-quantile",
    "0.9",
    "--min-volume-usd",
    "100000",
    "--exit-policy",
    "thesis",
    "--thesis-min-hold-hours",
    "1",
    "--thesis-score-retain",
    "0.6",
    "--thesis-min-score",
    "0.0001",
]


CONFIGS: dict[str, list[str]] = {
    "baseline": [],
    "anti_late_all": ["--anti-late-max-ret1-abs", "0.03", "--anti-late-max-ret3-abs", "0.06"],
    "anti_late_short": ["--entry-filter-side", "short", "--anti-late-max-ret1-abs", "0.03", "--anti-late-max-ret3-abs", "0.06"],
    "persistence_all": [
        "--entry-persistence-lookback",
        "3",
        "--entry-persistence-min-hits",
        "2",
        "--entry-score-retain-vs-lookback",
        "0.8",
        "--entry-max-signal-age",
        "2",
    ],
    "persistence_short": [
        "--entry-filter-side",
        "short",
        "--entry-persistence-lookback",
        "3",
        "--entry-persistence-min-hits",
        "2",
        "--entry-score-retain-vs-lookback",
        "0.8",
        "--entry-max-signal-age",
        "2",
    ],
    "slow_confirm_all": ["--require-slow-confirm"],
    "slow_confirm_short": ["--entry-filter-side", "short", "--require-slow-confirm"],
    "all_three_all": [
        "--anti-late-max-ret1-abs",
        "0.03",
        "--anti-late-max-ret3-abs",
        "0.06",
        "--entry-persistence-lookback",
        "3",
        "--entry-persistence-min-hits",
        "2",
        "--entry-score-retain-vs-lookback",
        "0.8",
        "--entry-max-signal-age",
        "2",
        "--require-slow-confirm",
    ],
    "all_three_short": [
        "--entry-filter-side",
        "short",
        "--anti-late-max-ret1-abs",
        "0.03",
        "--anti-late-max-ret3-abs",
        "0.06",
        "--entry-persistence-lookback",
        "3",
        "--entry-persistence-min-hits",
        "2",
        "--entry-score-retain-vs-lookback",
        "0.8",
        "--entry-max-signal-age",
        "2",
        "--require-slow-confirm",
    ],
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run C-Auto entry-filter A/B suite")
    p.add_argument("--start", default="2026-01-01")
    p.add_argument("--end", default="")
    p.add_argument("--configs", default="baseline,anti_late_all,anti_late_short,persistence_all,persistence_short,slow_confirm_all,slow_confirm_short,all_three_all,all_three_short")
    p.add_argument("--out-id", default="")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    suite_id = args.out_id or f"c_auto_entry_filter_ab_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    out_dir = OUT_ROOT / suite_id
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for name in [item.strip() for item in str(args.configs).split(",") if item.strip()]:
        if name not in CONFIGS:
            raise ValueError(f"unknown config {name}")
        run_id = f"{suite_id}_{name}"
        cmd = [sys.executable, "scripts/backtest_c_auto_v2_portfolio.py", "--out-id", run_id, *BASE_ARGS, *CONFIGS[name]]
        if args.start:
            cmd += ["--start", args.start]
        if args.end:
            cmd += ["--end", args.end]
        proc = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True)
        (out_dir / f"{name}.stdout.json").write_text(proc.stdout)
        (out_dir / f"{name}.stderr.txt").write_text(proc.stderr)
        if proc.returncode != 0:
            rows.append({"config": name, "status": "failed", "returncode": proc.returncode, "stderr_tail": proc.stderr[-2000:]})
            continue
        payload = json.loads(proc.stdout)
        metrics = payload.get("metrics") or {}
        rows.append(
            {
                "config": name,
                "status": metrics.get("status", "ok"),
                "trades": metrics.get("trades"),
                "win_rate": metrics.get("win_rate"),
                "total_return": metrics.get("total_return"),
                "annualized_return": metrics.get("annualized_return"),
                "max_drawdown": metrics.get("max_drawdown"),
                "sharpe_like": metrics.get("sharpe_like"),
                "final_nav": metrics.get("final_nav"),
                "out_id": run_id,
            }
        )
    result = {"generated_at": datetime.now(timezone.utc).isoformat(), "args": vars(args), "rows": rows}
    (out_dir / "summary.json").write_text(json.dumps(result, indent=2, sort_keys=True))
    (out_dir / "report.md").write_text(_markdown(result))
    print(json.dumps({"out_dir": str(out_dir.relative_to(ROOT)), **result}, indent=2, sort_keys=True))
    return 0


def _markdown(result: dict[str, Any]) -> str:
    lines = ["# C-Auto Entry Filter A/B", "", f"Generated: {result['generated_at']}", ""]
    lines.append("| Config | Trades | Win | Return | Max DD | Sharpe-like |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for row in result["rows"]:
        if row.get("status") == "failed":
            lines.append(f"| {row['config']} | failed | | | | |")
            continue
        lines.append(
            "| {config} | {trades} | {win_rate:.2%} | {total_return:.2%} | {max_drawdown:.2%} | {sharpe_like:.2f} |".format(
                config=row["config"],
                trades=int(row.get("trades") or 0),
                win_rate=float(row.get("win_rate") or 0.0),
                total_return=float(row.get("total_return") or 0.0),
                max_drawdown=float(row.get("max_drawdown") or 0.0),
                sharpe_like=float(row.get("sharpe_like") or 0.0),
            )
        )
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
