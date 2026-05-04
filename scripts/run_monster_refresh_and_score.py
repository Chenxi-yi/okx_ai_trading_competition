#!/usr/bin/env python3
"""Refresh latest monster data and rebuild the live watchlist.

This is a thin launcher-friendly wrapper. It never places orders.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT_ROOT = ROOT / "engine" / "data" / "monster_events"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Refresh OKX public data then score monster watchlist")
    p.add_argument("--run-prefix", default="monster_live")
    p.add_argument("--lookback-days", type=float, default=3.0)
    p.add_argument("--sleep-sec", type=float, default=0.25)
    p.add_argument("--min-quote-volume", type=float, default=1_000_000.0)
    p.add_argument("--min-score", type=float, default=0.75)
    p.add_argument("--max-ret-1h", type=float, default=0.25)
    p.add_argument("--fresh-hours", type=float, default=0.25)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    refresh_id = f"{args.run_prefix}_refresh_{stamp}"
    watchlist_id = f"{args.run_prefix}_watchlist_{stamp}"
    run_dir = OUT_ROOT / f"{args.run_prefix}_pipeline_{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    status_path = run_dir / "status.json"

    steps = [
        [
            sys.executable,
            "scripts/refresh_monster_latest.py",
            "--dataset-id",
            refresh_id,
            "--lookback-days",
            str(args.lookback_days),
            "--sleep-sec",
            str(args.sleep_sec),
        ],
        [
            sys.executable,
            "scripts/score_monster_watchlist.py",
            "--dataset-id",
            watchlist_id,
            "--market-snapshot",
            str(OUT_ROOT / refresh_id / "market_snapshot.csv"),
            "--min-quote-volume",
            str(args.min_quote_volume),
            "--min-score",
            str(args.min_score),
            "--max-ret-1h",
            str(args.max_ret_1h),
            "--fresh-hours",
            str(args.fresh_hours),
        ],
    ]

    manifest = {
        "dataset_id": run_dir.name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "refresh_id": refresh_id,
        "watchlist_id": watchlist_id,
        "steps": steps,
    }
    _write_json(status_path, {**manifest, "status": "running", "current_step": None})

    for i, cmd in enumerate(steps, start=1):
        _write_json(status_path, {**manifest, "status": "running", "current_step": i, "command": cmd})
        result = subprocess.run(cmd, cwd=str(ROOT), text=True)
        if result.returncode != 0:
            _write_json(
                status_path,
                {**manifest, "status": "failed", "failed_step": i, "returncode": result.returncode},
            )
            return result.returncode

    _write_json(run_dir / "manifest.json", {**manifest, "status": "completed"})
    _write_json(status_path, {**manifest, "status": "completed"})
    print(json.dumps({**manifest, "status": "completed"}, indent=2, sort_keys=True))
    return 0


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    raise SystemExit(main())
