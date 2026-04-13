#!/usr/bin/env python3
"""
Fetch and display current open swap positions.
Usage: python3 check_positions.py [--profile demo|live] [--json]
"""
import argparse, json, subprocess, sys
from pathlib import Path

LOG_ERROR = Path(__file__).resolve().parent / "log_error.py"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--profile", default="demo")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    cmd = ["okx", "--profile", args.profile, "--json", "account", "positions",
           "--instType", "SWAP"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except FileNotFoundError:
        print("ERROR: okx CLI not found", file=sys.stderr); sys.exit(1)

    if r.returncode != 0:
        err = r.stderr.strip() or r.stdout.strip()
        _log("CHECK_POSITIONS_FAILED", err, {"profile": args.profile})
        print(json.dumps({"success": False, "error": err})); sys.exit(1)

    data = json.loads(r.stdout) if r.stdout.strip() else {}
    if args.json:
        print(json.dumps(data, indent=2)); return

    positions = data.get("data", []) if isinstance(data, dict) else data
    if not positions:
        print(f"[{args.profile}] No open positions.")
        return

    print(f"\n[{args.profile}] Open Swap Positions ({len(positions)})")
    print(f"{'─'*75}")
    fmt = "  {:<16} {:>6} {:>10} {:>12} {:>12} {:>10}"
    print(fmt.format("instId", "side", "sz", "avgPx", "markPx", "uPnL"))
    print(f"{'─'*75}")
    for pos in positions:
        inst  = pos.get("instId", "?")
        side  = "LONG" if float(pos.get("pos", 0)) > 0 else "SHORT"
        sz    = abs(float(pos.get("pos", 0)))
        avgpx = float(pos.get("avgPx", 0))
        mkpx  = float(pos.get("markPx", 0))
        upnl  = float(pos.get("upl", 0))
        sign  = "+" if upnl >= 0 else ""
        print(fmt.format(inst, side, f"{sz:.4f}", f"{avgpx:,.2f}", f"{mkpx:,.2f}", f"{sign}{upnl:.2f}"))
    print(f"{'─'*75}\n")


def _log(code, msg, context=None):
    try:
        subprocess.run(["python3", str(LOG_ERROR), "--code", code, "--msg", msg,
                        "--context", json.dumps(context or {})], capture_output=True, timeout=5)
    except Exception: pass


if __name__ == "__main__":
    main()
