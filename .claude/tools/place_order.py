#!/usr/bin/env python3
"""
Place a perpetual swap order via OKX Agent Trade Kit CLI.
Tag 'agentTradeKit' is injected automatically. Default profile: demo.

Usage:
  python3 place_order.py --instId BTC-USDT-SWAP --side buy --sz 1
  python3 place_order.py --instId ETH-USDT-SWAP --side sell --sz 5 --profile live
  python3 place_order.py --instId BTC-USDT-SWAP --side buy --sz 1 --dry-run
  python3 place_order.py --instId BTC-USDT-SWAP --side buy --sz 1 --sl 60000 --tp 75000
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
LOG_ERROR = TOOLS_DIR / "log_error.py"

VALID_SWAP_SUFFIXES = ("-USDT-SWAP",)
VALID_SIDES = ("buy", "sell")


def validate(args):
    errors = []
    if not any(args.instId.endswith(s) for s in VALID_SWAP_SUFFIXES):
        errors.append(f"instId must end in -USDT-SWAP (got '{args.instId}'). See okx_api.md.")
    if args.side not in VALID_SIDES:
        errors.append(f"side must be buy or sell (got '{args.side}').")
    try:
        sz = int(args.sz)
        if sz < 1:
            errors.append("sz must be ≥ 1 contract.")
    except ValueError:
        errors.append(f"sz must be an integer (got '{args.sz}'). Remember: sz is CONTRACTS not coins.")
    if args.profile == "live":
        errors.append("LIVE PROFILE REQUIRES EXPLICIT USER AUTHORIZATION. Pass --profile live only when confirmed.")
    return errors


def main():
    p = argparse.ArgumentParser(description="Place OKX swap order via Agent Trade Kit")
    p.add_argument("--instId",   required=True,  help="e.g. BTC-USDT-SWAP")
    p.add_argument("--side",     required=True,  choices=["buy","sell"])
    p.add_argument("--sz",       required=True,  help="Number of contracts (NOT coins)")
    p.add_argument("--posSide",  default="net",  help="net (default for net position mode)")
    p.add_argument("--ordType",  default="market", choices=["market","limit","post_only"])
    p.add_argument("--px",       default=None,   help="Limit price (required for limit/post_only)")
    p.add_argument("--tdMode",   default="cross", choices=["cross","isolated"])
    p.add_argument("--tp",       default=None,   help="Take-profit trigger price")
    p.add_argument("--sl",       default=None,   help="Stop-loss trigger price")
    p.add_argument("--profile",  default="demo", help="demo (default) or live")
    p.add_argument("--dry-run",  action="store_true", help="Print command without executing")
    args = p.parse_args()

    # Validate
    errs = validate(args)
    # Allow live if explicitly passed (validation above just warns)
    if args.profile == "live" and "--profile live" not in " ".join(sys.argv):
        print(json.dumps({"success": False, "error": "live profile requires explicit --profile live flag"}))
        sys.exit(1)
    if any("LIVE PROFILE" not in e for e in errs) and any("LIVE PROFILE" in e for e in errs):
        errs = [e for e in errs if "LIVE PROFILE" not in e]  # filter live warning if intentional

    real_errors = [e for e in errs if "LIVE PROFILE" not in e]
    if real_errors:
        for e in real_errors:
            print(f"ERROR: {e}", file=sys.stderr)
        print(json.dumps({"success": False, "errors": real_errors}))
        sys.exit(1)

    # Build command
    cmd = [
        "okx", "--profile", args.profile, "--json",
        "swap", "place",
        "--instId",  args.instId,
        "--side",    args.side,
        "--ordType", args.ordType,
        "--sz",      str(args.sz),
        "--posSide", args.posSide,
        "--tdMode",  args.tdMode,
    ]
    if args.px:
        cmd += ["--px", args.px]
    if args.tp:
        cmd += ["--tpTriggerPx", args.tp, "--tpOrdPx", "-1"]
    if args.sl:
        cmd += ["--slTriggerPx", args.sl, "--slOrdPx", "-1"]

    if args.dry_run:
        print(f"DRY RUN — would execute:\n  {' '.join(cmd)}")
        sys.exit(0)

    print(f"Executing: {' '.join(cmd)}", file=sys.stderr)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        _log("ATK_NOT_FOUND", "okx CLI not found", {"cmd": cmd[0]})
        print(json.dumps({"success": False, "error": "okx CLI not found. Run: npm install -g @okx_ai/okx-trade-cli"}))
        sys.exit(1)

    if result.returncode != 0:
        err = result.stderr.strip() or result.stdout.strip()
        _log("PLACE_ORDER_FAILED", err, {"cmd": " ".join(cmd)})
        print(json.dumps({"success": False, "error": err}))
        sys.exit(1)

    data = json.loads(result.stdout) if result.stdout.strip() else {}
    print(json.dumps({"success": True, "profile": args.profile, "data": data}, indent=2))


def _log(code, msg, context=None):
    try:
        subprocess.run(
            ["python3", str(LOG_ERROR), "--code", code, "--msg", msg,
             "--context", json.dumps(context or {})],
            capture_output=True, timeout=5
        )
    except Exception:
        pass


if __name__ == "__main__":
    main()
