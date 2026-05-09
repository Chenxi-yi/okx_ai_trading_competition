#!/usr/bin/env python3
"""Continuous collector for OKX smart-money diffusion research data."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "engine" / "data" / "smartmoney_diffusion"
LOG_DIR = ROOT / "engine" / "logs" / "smartmoney_diffusion"
CONTROL_DIR = ROOT / "engine" / "control"
STATUS_PATH = LOG_DIR / "collector_status.json"
STOP_PATH = CONTROL_DIR / "smartmoney_diffusion_collector.stop"
PID_PATH = CONTROL_DIR / "smartmoney_diffusion_collector.pid"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run smart-money diffusion collection on a schedule")
    parser.add_argument("--symbols", default="auto", help="Comma-separated ccys or auto")
    parser.add_argument("--max-symbols", type=int, default=80)
    parser.add_argument("--limit", type=int, default=72)
    parser.add_argument("--period", type=int, default=7, choices=[3, 7, 30, 90])
    parser.add_argument("--lmt-num", type=int, default=100)
    parser.add_argument("--interval-sec", type=int, default=3600)
    parser.add_argument("--max-cycles", type=int, default=0, help="0 means run forever")
    parser.add_argument("--retry-previous-hour", action="store_true", default=True)
    return parser.parse_args()


def utc_hour(offset_hours: int = 0) -> str:
    ts = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    ts += timedelta(hours=offset_hours)
    return ts.strftime("%Y%m%d%H")


def write_status(payload: dict[str, Any]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    payload = dict(payload)
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    STATUS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def run_research(args: argparse.Namespace, as_of: str) -> dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "scripts/research_smartmoney_diffusion.py",
        "--symbols",
        args.symbols,
        "--max-symbols",
        str(args.max_symbols),
        "--as-of",
        as_of,
        "--limit",
        str(args.limit),
        "--period",
        str(args.period),
        "--lmt-num",
        str(args.lmt_num),
        "--output-dir",
        str(DATA_DIR),
    ]
    started_at = datetime.now(timezone.utc).isoformat()
    proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, timeout=900)
    stdout = proc.stdout.strip()
    stderr = proc.stderr.strip()
    report_rel = ""
    if proc.returncode == 0:
        for line in reversed(stdout.splitlines()):
            candidate = line.strip()
            if candidate.endswith("smartmoney_diffusion_report.md"):
                report_rel = candidate
                break
    report_path = ROOT / report_rel if report_rel else None
    run_dir = report_path.parent if report_path else None
    manifest = {}
    if run_dir and (run_dir / "manifest.json").exists():
        try:
            manifest = json.loads((run_dir / "manifest.json").read_text())
        except Exception:
            manifest = {}
    return {
        "returncode": proc.returncode,
        "ok": proc.returncode == 0,
        "as_of": as_of,
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "command": cmd,
        "stdout_tail": stdout.splitlines()[-10:],
        "stderr_tail": stderr.splitlines()[-10:],
        "report_path": report_rel or None,
        "run_dir": str(run_dir.relative_to(ROOT)) if run_dir else None,
        "manifest": manifest,
    }


def sleep_interruptibly(seconds: int) -> bool:
    deadline = time.time() + max(1, seconds)
    while time.time() < deadline:
        if STOP_PATH.exists():
            return True
        time.sleep(min(5.0, max(0.2, deadline - time.time())))
    return STOP_PATH.exists()


def main() -> int:
    args = parse_args()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if STOP_PATH.exists():
        STOP_PATH.unlink()
    PID_PATH.write_text(str(os.getpid()) + "\n")

    cycles = 0
    last_run: dict[str, Any] | None = None
    write_status(
        {
            "ok": True,
            "running": True,
            "pid": os.getpid(),
            "cycles": cycles,
            "interval_sec": args.interval_sec,
            "data_dir": str(DATA_DIR.relative_to(ROOT)),
            "last_run": last_run,
        }
    )
    try:
        while True:
            cycles += 1
            as_of = utc_hour()
            write_status(
                {
                    "ok": True,
                    "running": True,
                    "pid": os.getpid(),
                    "cycles": cycles,
                    "phase": "collecting",
                    "as_of": as_of,
                    "interval_sec": args.interval_sec,
                    "data_dir": str(DATA_DIR.relative_to(ROOT)),
                    "last_run": last_run,
                }
            )
            last_run = run_research(args, as_of)
            if not last_run["ok"] and args.retry_previous_hour:
                retry = run_research(args, utc_hour(-1))
                if retry["ok"]:
                    last_run = retry
                else:
                    last_run["retry_previous_hour"] = retry
            write_status(
                {
                    "ok": bool(last_run.get("ok")),
                    "running": True,
                    "pid": os.getpid(),
                    "cycles": cycles,
                    "phase": "sleeping",
                    "interval_sec": args.interval_sec,
                    "data_dir": str(DATA_DIR.relative_to(ROOT)),
                    "last_run": last_run,
                }
            )
            if args.max_cycles and cycles >= args.max_cycles:
                break
            if sleep_interruptibly(args.interval_sec):
                break
    finally:
        write_status(
            {
                "ok": bool(last_run and last_run.get("ok")),
                "running": False,
                "pid": os.getpid(),
                "cycles": cycles,
                "phase": "stopped",
                "interval_sec": args.interval_sec,
                "data_dir": str(DATA_DIR.relative_to(ROOT)),
                "last_run": last_run,
            }
        )
        if PID_PATH.exists() and PID_PATH.read_text().strip() == str(os.getpid()):
            PID_PATH.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
