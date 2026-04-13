#!/usr/bin/env python3
"""
Read engine/logs/summary.json and print current trading status.
Usage: python3 trading_status.py [--json]
"""
import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SUMMARY_FILE = PROJECT_ROOT / "engine" / "logs" / "summary.json"


def main():
    p = argparse.ArgumentParser(description="Show current trading status")
    p.add_argument("--json", action="store_true", help="Output raw JSON")
    args = p.parse_args()

    if not SUMMARY_FILE.exists():
        print("No summary.json found. Engine has not started yet.", file=sys.stderr)
        print('Run: python3 engine/main.py competition demo-start --strategy <id>')
        sys.exit(1)

    with open(SUMMARY_FILE) as f:
        summary = json.load(f)

    if args.json:
        print(json.dumps(summary, indent=2))
        return

    updated = summary.get("updated_at", "?")
    status  = summary.get("engine_status", "unknown").upper()
    pid     = summary.get("pid", "?")

    print(f"\n{'='*58}")
    print(f"  ENGINE STATUS: {status}  PID={pid}")
    print(f"  Updated: {updated}")
    print(f"{'='*58}")

    portfolios = summary.get("portfolios", {})
    if not portfolios:
        print("  No active portfolios.")
    else:
        for pid_name, snap in portfolios.items():
            nav     = snap.get("nav", 0)
            capital = snap.get("capital", 0)
            pnl     = snap.get("pnl", 0)
            pnl_pct = snap.get("pnl_pct", 0)
            dd      = snap.get("drawdown_pct", 0)
            n_pos   = snap.get("n_positions", 0)
            risk    = snap.get("risk", {})
            sign    = "+" if pnl >= 0 else ""
            print(f"\n  [{pid_name}]")
            print(f"    NAV:      ${nav:>9,.2f}  (capital ${capital:,.2f})")
            print(f"    PnL:      {sign}${pnl:>8,.2f}  ({sign}{pnl_pct:.2f}%)")
            print(f"    Drawdown: {dd:+.1f}%  |  Positions: {n_pos}")
            print(f"    Risk:     CB={risk.get('cb','?')}  Vol={risk.get('vol','?')}")
            last_reb = snap.get("last_rebalance", "Never")
            print(f"    Last reb: {last_reb}")

    total_nav = summary.get("total_nav", 0)
    total_pnl = summary.get("total_pnl", 0)
    total_pct = summary.get("total_pnl_pct", 0)
    sign = "+" if total_pnl >= 0 else ""
    print(f"\n{'─'*58}")
    print(f"  TOTAL NAV: ${total_nav:,.2f}  PnL: {sign}${total_pnl:,.2f} ({sign}{total_pct:.2f}%)")
    print(f"{'='*58}\n")

    if status != "RUNNING":
        sys.exit(1)


if __name__ == "__main__":
    main()
