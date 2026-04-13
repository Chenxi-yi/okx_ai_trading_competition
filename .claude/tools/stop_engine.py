#!/usr/bin/env python3
"""
Gracefully stop the trading daemon. Prints final NAV before stopping.
Usage: python3 stop_engine.py [--force]
REQUIRES: user confirmation for live trading.
"""
import argparse, json, os, signal, sys, time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
ENGINE_DIR   = PROJECT_ROOT / "engine"
PID_FILE     = ENGINE_DIR / "control" / "trading.pid"
SUMMARY_FILE = ENGINE_DIR / "logs" / "summary.json"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--force", action="store_true", help="SIGKILL if graceful stop fails")
    args = p.parse_args()

    if not PID_FILE.exists():
        print("No PID file. Engine is not running.")
        sys.exit(1)

    pid = int(PID_FILE.read_text().strip())

    # Print final NAV snapshot
    if SUMMARY_FILE.exists():
        with open(SUMMARY_FILE) as f:
            summary = json.load(f)
        nav = summary.get("total_nav", 0)
        pnl = summary.get("total_pnl", 0)
        pct = summary.get("total_pnl_pct", 0)
        sign = "+" if pnl >= 0 else ""
        print(f"Final snapshot — NAV: ${nav:,.2f}  PnL: {sign}${pnl:,.2f} ({sign}{pct:.2f}%)")

    print(f"Sending SIGTERM to PID {pid}...")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        print(f"PID {pid} not found. Cleaning up stale PID file.")
        PID_FILE.unlink(missing_ok=True)
        sys.exit(0)

    # Wait for graceful shutdown
    for i in range(15):
        time.sleep(1)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            PID_FILE.unlink(missing_ok=True)
            print("Engine stopped.")
            return

    if args.force:
        print(f"Still running after 15s. Sending SIGKILL...")
        os.kill(pid, signal.SIGKILL)
        PID_FILE.unlink(missing_ok=True)
    else:
        print(f"Engine still running after 15s. Use --force to kill, or: kill -9 {pid}")
        sys.exit(1)


if __name__ == "__main__":
    main()
