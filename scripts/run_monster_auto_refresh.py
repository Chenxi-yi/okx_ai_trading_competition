#!/usr/bin/env python3
"""Periodic monster watchlist refresh loop.

This script repeatedly runs the existing refresh-and-score pipeline and writes
append-only progress/status files. It never places orders.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUT_ROOT = ROOT / "engine" / "data" / "monster_events"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run monster refresh-and-score periodically")
    p.add_argument("--run-id", default=None)
    p.add_argument("--interval-sec", type=float, default=900.0)
    p.add_argument("--iterations", type=int, default=0, help="0 means continuous")
    p.add_argument("--run-prefix", default="monster_auto")
    p.add_argument("--lookback-days", type=float, default=3.0)
    p.add_argument("--sleep-sec", type=float, default=0.25)
    p.add_argument("--min-quote-volume", type=float, default=1_000_000.0)
    p.add_argument("--min-score", type=float, default=0.75)
    p.add_argument("--max-ret-1h", type=float, default=0.25)
    p.add_argument("--fresh-hours", type=float, default=0.35)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    run_id = args.run_id or f"monster_auto_refresh_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    out_dir = OUT_ROOT / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    status_path = out_dir / "status.json"
    progress_path = out_dir / "progress.jsonl"
    manifest = {
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "interval_sec": args.interval_sec,
        "iterations": args.iterations,
        "run_prefix": args.run_prefix,
        "lookback_days": args.lookback_days,
        "sleep_sec": args.sleep_sec,
        "min_quote_volume": args.min_quote_volume,
        "min_score": args.min_score,
        "max_ret_1h": args.max_ret_1h,
        "fresh_hours": args.fresh_hours,
        "artifacts": {
            "progress_jsonl": _relpath(progress_path),
            "status_json": _relpath(status_path),
        },
    }
    _write_json(out_dir / "manifest.json", {**manifest, "status": "running"})

    iteration = 0
    ok = 0
    failed = 0
    while args.iterations == 0 or iteration < args.iterations:
        iteration += 1
        started = datetime.now(timezone.utc).isoformat()
        cmd = [
            sys.executable,
            "scripts/run_monster_refresh_and_score.py",
            "--run-prefix",
            args.run_prefix,
            "--lookback-days",
            str(args.lookback_days),
            "--sleep-sec",
            str(args.sleep_sec),
            "--min-quote-volume",
            str(args.min_quote_volume),
            "--min-score",
            str(args.min_score),
            "--max-ret-1h",
            str(args.max_ret_1h),
            "--fresh-hours",
            str(args.fresh_hours),
        ]
        _write_json(
            status_path,
            {
                **manifest,
                "status": "running",
                "iteration": iteration,
                "ok": ok,
                "failed": failed,
                "current_command": cmd,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        result = subprocess.run(cmd, cwd=str(ROOT), text=True)
        record: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "iteration": iteration,
            "started_at": started,
            "returncode": result.returncode,
        }
        if result.returncode == 0:
            ok += 1
            record["status"] = "ok"
        else:
            failed += 1
            record["status"] = "failed"
        _append_jsonl(progress_path, record)
        _write_json(
            status_path,
            {
                **manifest,
                "status": "running" if args.iterations == 0 or iteration < args.iterations else "completed",
                "iteration": iteration,
                "ok": ok,
                "failed": failed,
                "last_record": record,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        if args.iterations != 0 and iteration >= args.iterations:
            break
        time.sleep(max(args.interval_sec, 30.0))

    final = {**manifest, "status": "completed", "ok": ok, "failed": failed, "completed_at": datetime.now(timezone.utc).isoformat()}
    _write_json(out_dir / "manifest.json", final)
    _write_json(status_path, final)
    print(json.dumps(final, indent=2, sort_keys=True))
    return 0 if failed == 0 else 1


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    with path.open("a") as fh:
        fh.write(json.dumps(payload, sort_keys=True, default=str) + "\n")


def _relpath(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


if __name__ == "__main__":
    raise SystemExit(main())
