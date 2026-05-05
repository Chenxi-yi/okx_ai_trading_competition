#!/usr/bin/env python3
"""Run the C-Auto v2 sleeve screening experiments from a policy JSON file."""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
ENGINE_DIR = ROOT / "engine"
DEFAULT_POLICY = ENGINE_DIR / "strategies" / "specs" / "c_auto_v2_regime_policy.json"
DEFAULT_SUMMARY_ID = "c_auto_v2_sleeve_experiments_v1"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run C-Auto v2 sleeve experiments")
    p.add_argument("--policy", default=str(DEFAULT_POLICY))
    p.add_argument("--summary-id", default=DEFAULT_SUMMARY_ID)
    p.add_argument("--dataset-id", default="")
    p.add_argument("--max-folds", type=int, default=-1, help="-1 uses policy default; 0 means all folds")
    p.add_argument("--only-sleeve", default="", help="Optional sleeve_id filter")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    policy_path = Path(args.policy)
    policy = json.loads(policy_path.read_text())
    dataset_id = args.dataset_id or policy["dataset_id"]
    max_folds = int(policy.get("default_max_folds", 0)) if args.max_folds < 0 else args.max_folds
    summary_dir = ENGINE_DIR / "data" / "research" / "c_auto" / args.summary_id
    summary_dir.mkdir(parents=True, exist_ok=True)

    runs = []
    for sleeve in policy.get("sleeves", []):
        sleeve_id = str(sleeve["sleeve_id"])
        if args.only_sleeve and sleeve_id != args.only_sleeve:
            continue
        for exp in sleeve.get("experiments", []):
            runs.append((sleeve, exp))

    if args.dry_run:
        print(json.dumps([exp for _, exp in runs], indent=2, sort_keys=True))
        return 0

    summary_rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for sleeve, exp in runs:
        row = _run_one(
            policy=policy,
            sleeve=sleeve,
            exp=exp,
            dataset_id=dataset_id,
            max_folds=max_folds,
        )
        if row.get("status") == "error":
            failures.append(row)
        summary_rows.append(row)
        print(json.dumps(row, sort_keys=True))

    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "summary_id": args.summary_id,
        "policy_id": policy.get("policy_id"),
        "policy_path": str(policy_path),
        "dataset_id": dataset_id,
        "max_folds": max_folds,
        "runs": len(summary_rows),
        "failures": failures,
        "summary": summary_rows,
    }
    (summary_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    _write_csv(summary_dir / "summary.csv", summary_rows)
    (summary_dir / "summary.md").write_text(_summary_markdown(manifest))

    if failures:
        return 1
    return 0


def _run_one(
    *,
    policy: dict[str, Any],
    sleeve: dict[str, Any],
    exp: dict[str, Any],
    dataset_id: str,
    max_folds: int,
) -> dict[str, Any]:
    out_id = str(exp["experiment_id"])
    cmd = [
        sys.executable,
        str(ENGINE_DIR / "research" / "c_auto_experiment.py"),
        "--dataset-id",
        dataset_id,
        "--out-id",
        out_id,
        "--label-col",
        str(exp["label_col"]),
        "--regime-col",
        str(policy["regime_column"]),
        "--regime-value",
        str(exp["regime"]),
        "--feature-columns",
        ",".join(exp["feature_columns"]),
        "--notes",
        f"c-auto v2 sleeve={sleeve['sleeve_id']} money_source={sleeve['money_source']} side_policy={exp['side_policy']}",
    ]
    if max_folds > 0:
        cmd.extend(["--max-folds", str(max_folds)])

    result = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
    if result.returncode != 0:
        return {
            "status": "error",
            "sleeve_id": sleeve["sleeve_id"],
            "money_source": sleeve["money_source"],
            "experiment_id": out_id,
            "regime": exp["regime"],
            "label_col": exp["label_col"],
            "side_policy": exp["side_policy"],
            "returncode": result.returncode,
            "stderr": result.stderr[-2000:],
            "stdout": result.stdout[-2000:],
        }

    metrics_path = ENGINE_DIR / "data" / "research" / "c_auto" / out_id / "metrics.json"
    metrics = json.loads(metrics_path.read_text())
    return {
        "status": metrics.get("status", "unknown"),
        "sleeve_id": sleeve["sleeve_id"],
        "money_source": sleeve["money_source"],
        "experiment_id": out_id,
        "regime": exp["regime"],
        "label_col": exp["label_col"],
        "side_policy": exp["side_policy"],
        "rows": metrics.get("rows", 0),
        "folds": metrics.get("folds", 0),
        "spearman_ic": metrics.get("spearman_ic", 0.0),
        "directional_accuracy": metrics.get("directional_accuracy", 0.0),
        "long_tail_mean_return": metrics.get("long_tail_mean_return", 0.0),
        "short_tail_mean_return": metrics.get("short_tail_mean_return", 0.0),
        "long_short_spread": metrics.get("long_short_spread", 0.0),
        "selected_mean_return": metrics.get("selected_mean_return", 0.0),
        "selection_rate": metrics.get("selection_rate", 0.0),
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("")
        return
    keys = list(rows[0].keys())
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


def _summary_markdown(manifest: dict[str, Any]) -> str:
    lines = [
        f"# {manifest['summary_id']}",
        "",
        f"Created: {manifest['created_at']}",
        f"Policy: `{manifest['policy_id']}`",
        f"Dataset: `{manifest['dataset_id']}`",
        f"Max folds: {manifest['max_folds']}",
        "",
        "| Sleeve | Regime | Label | IC | Spread | Long Tail | Short Tail | Rows | Folds |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    rows = sorted(
        manifest["summary"],
        key=lambda row: (str(row.get("sleeve_id")), str(row.get("regime")), str(row.get("label_col"))),
    )
    for row in rows:
        lines.append(
            "| {sleeve_id} | {regime} | {label_col} | {spearman_ic:.4f} | {long_short_spread:.4f} | "
            "{long_tail_mean_return:.4f} | {short_tail_mean_return:.4f} | {rows} | {folds} |".format(
                **_format_row(row)
            )
        )
    lines.append("")
    if manifest["failures"]:
        lines.append("## Failures")
        lines.append("")
        for failure in manifest["failures"]:
            lines.append(f"- `{failure['experiment_id']}`: returncode {failure['returncode']}")
        lines.append("")
    return "\n".join(lines)


def _format_row(row: dict[str, Any]) -> dict[str, Any]:
    formatted = dict(row)
    for key in ["spearman_ic", "long_short_spread", "long_tail_mean_return", "short_tail_mean_return"]:
        formatted[key] = float(formatted.get(key) or 0.0)
    formatted["rows"] = int(formatted.get("rows") or 0)
    formatted["folds"] = int(formatted.get("folds") or 0)
    return formatted


if __name__ == "__main__":
    raise SystemExit(main())
