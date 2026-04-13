#!/usr/bin/env python3
"""
Fetch account balance and display USDT equity.
Usage: python3 check_balance.py [--profile demo|live] [--json]
"""
import argparse, json, subprocess, sys
from pathlib import Path

LOG_ERROR = Path(__file__).resolve().parent / "log_error.py"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--profile", default="demo")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    cmd = ["okx", "--profile", args.profile, "--json", "account", "balance"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except FileNotFoundError:
        print("ERROR: okx CLI not found", file=sys.stderr); sys.exit(1)

    if r.returncode != 0:
        err = r.stderr.strip() or r.stdout.strip()
        _log("CHECK_BALANCE_FAILED", err, {"profile": args.profile})
        print(json.dumps({"success": False, "error": err})); sys.exit(1)

    data = json.loads(r.stdout) if r.stdout.strip() else {}
    if args.json:
        print(json.dumps(data, indent=2)); return

    # ATK --json returns a raw list (not {"data":[...]}). Handle both.
    raw = data if isinstance(data, list) else data.get("data", [{}])
    try:
        details = raw[0] if raw else {}
        def _f(v, default=0.0):
            """Convert OKX value to float — handles empty strings."""
            try: return float(v) if v != "" else default
            except (TypeError, ValueError): return default

        total_eq = _f(details.get("totalEq"))
        avail_eq = _f(details.get("adjEq")) or total_eq
        used_eq  = total_eq - avail_eq
        util_pct = (used_eq / total_eq * 100) if total_eq > 0 else 0

        assets = details.get("details", [])
        usdt = next((a for a in assets if a.get("ccy") == "USDT"), {})
        usdt_bal   = _f(usdt.get("eq"))
        usdt_avail = _f(usdt.get("availEq"))
        upnl       = _f(details.get("upl"))

        print(f"\n[{args.profile}] Account Balance")
        print(f"  Total equity:  ${total_eq:>10,.2f} USDT")
        print(f"  Available:     ${usdt_avail:>10,.2f} USDT")
        print(f"  USDT balance:  ${usdt_bal:>10,.2f} USDT")
        sign = "+" if upnl >= 0 else ""
        print(f"  Unrealized PnL:{sign}${upnl:>9,.2f} USDT")
        print(f"  Utilization:   {util_pct:.1f}%\n")
    except Exception as e:
        print(f"Could not parse balance: {e}\nRaw: {json.dumps(data, indent=2)}")
        sys.exit(1)


def _log(code, msg, context=None):
    try:
        subprocess.run(["python3", str(LOG_ERROR), "--code", code, "--msg", msg,
                        "--context", json.dumps(context or {})], capture_output=True, timeout=5)
    except Exception: pass


if __name__ == "__main__":
    main()
