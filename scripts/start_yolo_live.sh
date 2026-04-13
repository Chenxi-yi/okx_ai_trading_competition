#!/bin/zsh
set -euo pipefail

ROOT="/Users/yichenxi/.openclaw/workspace/okx_ai_skill_challenage"
ENGINE_DIR="$ROOT/engine"
LOG_DIR="$ENGINE_DIR/logs"
CONTROL_DIR="$ENGINE_DIR/control"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
export PYTHONUNBUFFERED=1
export STRATEGY_PROFILE="live"
export LIVE_TRADING="true"
export YOLO_RESET_STATE="${YOLO_RESET_STATE:-0}"

mkdir -p "$LOG_DIR" "$CONTROL_DIR"
rm -f "$CONTROL_DIR/yolo_momentum.pid"

balance_json="$(okx --profile live --json account balance)"
budget="$(
  printf '%s' "$balance_json" | python3 -c '
import json
import sys

raw = sys.stdin.read().strip()
data = json.loads(raw) if raw else []
entries = data if isinstance(data, list) else []
for entry in entries:
    for detail in entry.get("details", []):
        if detail.get("ccy") == "USDT":
            print(detail.get("availBal") or detail.get("availEq") or detail.get("cashBal") or "0")
            raise SystemExit(0)
print("0")
'
)"
export YOLO_TOTAL_BUDGET="${YOLO_TOTAL_BUDGET:-$budget}"

cd "$ROOT"
exec python3 engine/main.py competition demo-start --strategy yolo_momentum --foreground
