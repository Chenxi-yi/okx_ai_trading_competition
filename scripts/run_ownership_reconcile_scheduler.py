#!/usr/bin/env python3
"""Periodically refresh live ownership reconciliation and performance."""

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
ENGINE_DIR = ROOT / "engine"
LOG_DIR = ENGINE_DIR / "logs" / "ownership"
CONTROL_DIR = ENGINE_DIR / "control"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run ownership reconciliation scheduler")
    parser.add_argument("--environments", default="personal,competition")
    parser.add_argument("--interval-sec", type=float, default=300.0)
    parser.add_argument("--max-cycles", type=int, default=0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    environments = [item.strip() for item in str(args.environments).split(",") if item.strip()]
    environments = [item for item in environments if item in {"personal", "competition"}]
    if not environments:
        raise SystemExit("no supported environments")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    stop_path = CONTROL_DIR / "ownership_reconcile_scheduler.stop"
    try:
        stop_path.unlink()
    except FileNotFoundError:
        pass
    cycles = 0
    while True:
        cycles += 1
        results = {}
        for env in environments:
            results[env] = _run_reconcile(env)
        _write_status("running", cycles, results, args)
        if args.max_cycles and cycles >= int(args.max_cycles):
            _write_status("completed", cycles, results, args)
            return 0
        if stop_path.exists():
            _write_status("stopped", cycles, results, args)
            return 0
        time.sleep(max(5.0, float(args.interval_sec)))


def _run_reconcile(environment: str) -> dict[str, Any]:
    profile = "live" if environment == "competition" else environment
    cmd = [
        sys.executable,
        "scripts/reconcile_live_ownership.py",
        "--environment",
        environment,
        "--okx-profile",
        profile,
        "--write-status",
        "--json",
    ]
    started = datetime.now(timezone.utc).isoformat()
    try:
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=90)
    except Exception as exc:
        return {"ok": False, "started_at": started, "error": str(exc)}
    payload: dict[str, Any] = {}
    if proc.stdout.strip():
        try:
            parsed = json.loads(proc.stdout)
            if isinstance(parsed, dict):
                payload = parsed
        except json.JSONDecodeError:
            payload = {}
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "owned_count": payload.get("owned_count"),
            "exchange_count": payload.get("exchange_count"),
            "errors": payload.get("errors"),
            "performance_strategies": len((payload.get("performance") or {}).get("strategies") or []),
        },
        "stderr": (proc.stderr or "")[-1000:],
    }


def _write_status(status: str, cycles: int, results: dict[str, Any], args: argparse.Namespace) -> None:
    path = LOG_DIR / "scheduler_status.json"
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "cycles": cycles,
        "interval_sec": float(args.interval_sec),
        "environments": list(results),
        "results": results,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
