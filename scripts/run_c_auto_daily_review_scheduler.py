#!/usr/bin/env python3
"""Run the C-Auto daily review generator once per local day."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "engine" / "logs" / "c_auto_v2_paper"
STATUS_PATH = LOG_DIR / "daily_review_scheduler.json"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="C-Auto daily review scheduler")
    p.add_argument("--state-id", default="fixed1000_conservative")
    p.add_argument("--environment", default="competition")
    p.add_argument("--time", default="23:58", help="Asia/Shanghai HH:MM generation time")
    p.add_argument("--run-on-start", action="store_true")
    return p.parse_args()


def write_status(status: str, args: argparse.Namespace, **extra: object) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": status,
        "state_id": args.state_id,
        "environment": args.environment,
        "review_time": args.time,
        "heartbeat_at": datetime.now(LOCAL_TZ).isoformat(timespec="seconds"),
    }
    payload.update(extra)
    STATUS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def next_run_at(hhmm: str, now: datetime | None = None) -> datetime:
    now = now or datetime.now(LOCAL_TZ)
    hour_text, minute_text = hhmm.split(":", 1)
    target = now.replace(hour=int(hour_text), minute=int(minute_text), second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def run_review(args: argparse.Namespace) -> tuple[int, str]:
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "generate_c_auto_daily_review.py"),
        "--state-id",
        args.state_id,
        "--environment",
        args.environment,
    ]
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=120)
    output = (proc.stdout + proc.stderr).strip()
    return proc.returncode, output


def main() -> int:
    args = parse_args()
    last_run_date: str | None = None
    if args.run_on_start:
        rc, output = run_review(args)
        last_run_date = datetime.now(LOCAL_TZ).date().isoformat() if rc == 0 else None
        write_status("running", args, last_returncode=rc, last_output=output, last_run_date=last_run_date)
    while True:
        target = next_run_at(args.time)
        while True:
            now = datetime.now(LOCAL_TZ)
            sleep_sec = max(1.0, min(300.0, (target - now).total_seconds()))
            write_status("waiting", args, next_run_at=target.isoformat(), last_run_date=last_run_date)
            time.sleep(sleep_sec)
            if datetime.now(LOCAL_TZ) >= target:
                break
        today = datetime.now(LOCAL_TZ).date().isoformat()
        if last_run_date == today:
            continue
        rc, output = run_review(args)
        if rc == 0:
            last_run_date = today
            write_status("running", args, last_returncode=rc, last_output=output, last_run_date=last_run_date)
        else:
            write_status("error", args, last_returncode=rc, last_output=output, last_run_date=last_run_date)
            time.sleep(300)


if __name__ == "__main__":
    raise SystemExit(main())
