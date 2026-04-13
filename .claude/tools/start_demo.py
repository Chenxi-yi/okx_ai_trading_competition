#!/usr/bin/env python3
"""
Start a competition strategy in demo mode.
Usage: python3 start_demo.py <strategy_id> [--foreground]
"""
import argparse, json, subprocess, sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
ENGINE_DIR   = PROJECT_ROOT / "engine"
PID_FILE     = ENGINE_DIR / "control" / "trading.pid"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("strategy_id")
    p.add_argument("--foreground", "-f", action="store_true")
    args = p.parse_args()

    # Check if engine already running
    if PID_FILE.exists():
        pid = PID_FILE.read_text().strip()
        print(f"Engine already running (PID={pid}). Run stop_engine.py first.")
        sys.exit(1)

    # Validate strategy exists
    sys.path.insert(0, str(ENGINE_DIR))
    from competition.registry import CompetitionRegistry
    reg = CompetitionRegistry()
    if not reg.exists(args.strategy_id):
        print(f"Unknown strategy: {args.strategy_id!r}")
        print(f"Available: {', '.join(reg.ids())}")
        sys.exit(1)

    cfg     = reg.get(args.strategy_id)
    seed    = cfg.get("seed_capital", 300)
    current = cfg.get("current_capital", seed)
    print(f"Starting demo: [{args.strategy_id}] {cfg['name']}")
    print(f"Capital: {current} USDT (seed: {seed} USDT) | Profile: {cfg['base_profile']}")

    cmd = ["python3", "main.py", "competition", "demo-start", "--strategy", args.strategy_id]
    if args.foreground:
        cmd.append("--foreground")

    result = subprocess.run(cmd, cwd=str(ENGINE_DIR))
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
