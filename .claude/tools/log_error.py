#!/usr/bin/env python3
"""
Append a structured error record to the persistent error registry.
This is the self-update tool — run after EVERY error encountered and fixed.

Usage:
  python3 log_error.py --code ATK_NOT_FOUND --msg "okx CLI not on PATH"
  python3 log_error.py --code NET_MODE_REQUIRED --msg "posSide error" --context '{"instId":"BTC-USDT-SWAP"}'
  python3 log_error.py --code NET_MODE_REQUIRED --msg "RESOLVED" --context '{"resolved":true,"fix":"run set-position-mode net"}'
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REGISTRY = Path(__file__).resolve().parent.parent / "errors" / "registry.jsonl"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--code",    required=True,  help="Unique error code (UPPER_SNAKE_CASE)")
    p.add_argument("--msg",     required=True,  help="Error description")
    p.add_argument("--context", default="{}",   help="JSON context dict")
    args = p.parse_args()

    try:
        context = json.loads(args.context)
    except json.JSONDecodeError:
        context = {"raw": args.context}

    record = {
        "ts":       datetime.now(timezone.utc).isoformat(),
        "code":     args.code,
        "msg":      args.msg,
        "context":  context,
        "resolved": context.get("resolved", False),
    }

    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    with open(REGISTRY, "a") as f:
        f.write(json.dumps(record) + "\n")

    status = "RESOLVED" if record["resolved"] else "LOGGED"
    print(f"[{status}] {args.code} — {args.msg}")
    print(f"  Registry: {REGISTRY}")


if __name__ == "__main__":
    main()
