#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENGINE_DIR="$ROOT_DIR/engine"
LOG_DIR="$ENGINE_DIR/logs"
CONTROL_DIR="$ENGINE_DIR/control"

STRATEGY="${1:-elite_flow}"
arg2="${2:-}"
arg3="${3:-}"

if [[ "$arg2" == "demo" || "$arg2" == "live" ]]; then
  ENV_MODE="$arg2"
  DASHBOARD_PORT="${arg3:-8080}"
else
  DASHBOARD_PORT="${arg2:-8080}"
  ENV_MODE="${arg3:-demo}"
fi

case "$STRATEGY" in
  elite_flow|yolo_momentum|yolo_orchestrator) ;;
  *)
    echo "unknown strategy: $STRATEGY"
    echo "usage: ./start_local.sh [elite_flow|yolo_momentum|yolo_orchestrator] [port] [demo|live]"
    exit 1
    ;;
esac

case "$ENV_MODE" in
  demo|live) ;;
  *)
    echo "unknown environment: $ENV_MODE"
    echo "usage: ./start_local.sh [elite_flow|yolo_momentum|yolo_orchestrator] [port] [demo|live]"
    exit 1
    ;;
esac

STRATEGY_LOG="$LOG_DIR/${STRATEGY}_${ENV_MODE}.stdout.log"
STRATEGY_PID_FILE="$CONTROL_DIR/${STRATEGY}.pid"
DASHBOARD_LOG="$LOG_DIR/dashboard_${DASHBOARD_PORT}.stdout.log"
DASHBOARD_PID_FILE="$CONTROL_DIR/dashboard.pid"

mkdir -p "$LOG_DIR" "$CONTROL_DIR"

is_running() {
  local pid="${1:-}"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

find_strategy_pid() {
  pgrep -f "engine/main.py competition demo-start --strategy $STRATEGY --foreground" | tail -n 1 || true
}

find_dashboard_pid() {
  pgrep -f "python3 dashboard.py --port $DASHBOARD_PORT" | tail -n 1 || true
}

read_pid() {
  local file="$1"
  [[ -f "$file" ]] && tr -d '[:space:]' < "$file" || true
}

check_http() {
  local url="$1"
  curl -sSf "$url" >/dev/null 2>&1
}

wait_for_http() {
  local url="$1"
  local attempts="${2:-10}"
  local delay_sec="${3:-1}"
  local i
  for i in $(seq 1 "$attempts"); do
    if check_http "$url"; then
      return 0
    fi
    sleep "$delay_sec"
  done
  return 1
}

summary_field() {
  local field="$1"
  python3 - "$field" <<'PY'
import json
import sys
from pathlib import Path
import os
import signal

field = sys.argv[1]
path = Path("engine/logs/summary.json")
if not path.exists():
    raise SystemExit(0)
try:
    data = json.loads(path.read_text())
    pid = data.get("pid")
    if pid is not None:
        try:
            os.kill(int(pid), 0)
        except Exception:
            raise SystemExit(0)
    value = data.get(field)
    if value is not None:
        print(value)
except Exception:
    pass
PY
}

summary_matches_pid() {
  local expected_pid="$1"
  python3 - "$expected_pid" <<'PY'
import json
import sys
from pathlib import Path

expected = str(sys.argv[1]).strip()
path = Path("engine/logs/summary.json")
if not path.exists() or not expected:
    raise SystemExit(1)
try:
    data = json.loads(path.read_text())
    actual = str(data.get("pid", "")).strip()
    raise SystemExit(0 if actual == expected else 1)
except Exception:
    raise SystemExit(1)
PY
}

current_strategy="$(summary_field strategy)"
current_pid="$(summary_field pid)"
actual_strategy_pid="$(find_strategy_pid)"
actual_dashboard_pid="$(find_dashboard_pid)"

okx_balance_json() {
  okx --profile "$ENV_MODE" --json account balance 2>/dev/null || return 1
}

okx_positions_json() {
  okx --profile "$ENV_MODE" --json account positions 2>/dev/null || return 1
}

extract_usdt_avail() {
  python3 -c '
import json
import sys

raw = sys.stdin.read().strip()
if not raw:
    raise SystemExit(1)
data = json.loads(raw)
entries = data if isinstance(data, list) else []
for entry in entries:
    for detail in entry.get("details", []):
        if detail.get("ccy") == "USDT":
            print(detail.get("availBal") or detail.get("availEq") or detail.get("cashBal") or "0")
            raise SystemExit(0)
raise SystemExit(1)
'
}

count_open_positions() {
  python3 -c '
import json
import sys

raw = sys.stdin.read().strip()
if not raw:
    print(0)
    raise SystemExit(0)
data = json.loads(raw)
items = data if isinstance(data, list) else []
count = 0
for p in items:
    try:
        if abs(float(p.get("pos") or 0)) > 0:
            count += 1
    except Exception:
        pass
print(count)
'
}

precheck_environment() {
  echo "precheck: verifying $ENV_MODE profile ..."
  local balance_json
  if ! balance_json="$(okx_balance_json)"; then
    echo "precheck failed: cannot read account balance for profile=$ENV_MODE"
    exit 1
  fi

  local usdt_avail="0"
  usdt_avail="$(printf '%s' "$balance_json" | extract_usdt_avail 2>/dev/null || echo "0")"
  echo "precheck: $ENV_MODE USDT available = ${usdt_avail}"

  local pos_json pos_count
  pos_json="$(okx_positions_json || echo "[]")"
  pos_count="$(printf '%s' "$pos_json" | count_open_positions)"
  echo "precheck: $ENV_MODE open positions = ${pos_count}"

  if ! okx --profile "$ENV_MODE" --json market ticker BTC-USDT-SWAP >/dev/null 2>&1; then
    echo "precheck failed: market ticker request failed for profile=$ENV_MODE"
    exit 1
  fi
  echo "precheck: market data access OK"

  if [[ "$STRATEGY" == yolo_* ]]; then
    export YOLO_TOTAL_BUDGET="${YOLO_TOTAL_BUDGET:-$usdt_avail}"
    echo "precheck: YOLO budget cap = ${YOLO_TOTAL_BUDGET}"
  fi
}

if [[ -n "${current_strategy:-}" && "$current_strategy" != "$STRATEGY" ]] && is_running "${current_pid:-}"; then
  echo "another strategy is already running: $current_strategy (pid=$current_pid)"
  echo "stop it first with ./stop_local.sh"
  exit 1
fi

if [[ -n "${actual_strategy_pid:-}" ]] && [[ "$current_strategy" != "$STRATEGY" ]] && is_running "${actual_strategy_pid:-}"; then
  echo "another strategy is already running: $current_strategy (pid=$actual_strategy_pid)"
  echo "stop it first with ./stop_local.sh"
  exit 1
fi

start_strategy() {
  local pid
  pid="$(find_strategy_pid)"
  [[ -z "$pid" ]] && pid="$(read_pid "$STRATEGY_PID_FILE")"
  [[ -z "$pid" ]] && pid="$current_pid"

  if is_running "$pid"; then
    echo "$STRATEGY already running (pid=$pid)"
    echo "$pid" > "$STRATEGY_PID_FILE"
    return
  fi

  echo "starting $STRATEGY in $ENV_MODE mode..."
  (
    cd "$ROOT_DIR"
    STRATEGY_PROFILE="$ENV_MODE" \
    LIVE_TRADING="$([[ "$ENV_MODE" == "live" ]] && echo true || echo false)" \
    YOLO_RESET_STATE="$([[ "$ENV_MODE" == "live" && "$STRATEGY" == yolo_* ]] && echo 1 || echo 0)" \
    nohup python3 engine/main.py competition demo-start --strategy "$STRATEGY" --foreground \
      >> "$STRATEGY_LOG" 2>&1 < /dev/null &
    local child_pid=$!
    disown "$child_pid" 2>/dev/null || true
    echo "$child_pid" > "$STRATEGY_PID_FILE"
  )
  sleep 2
  pid="$(read_pid "$STRATEGY_PID_FILE")"
  if ! is_running "$pid"; then
    echo "failed to start $STRATEGY"
    echo "check log: $STRATEGY_LOG"
    rm -f "$STRATEGY_PID_FILE"
    exit 1
  fi
  echo "$STRATEGY pid: ${pid:-unknown}"

  # Give custom strategies a moment to refresh summary.json so the printed
  # status block doesn't report a stale PID from a previous run.
  local _try
  for _try in $(seq 1 8); do
    if summary_matches_pid "$pid"; then
      break
    fi
    sleep 1
  done
}

start_dashboard() {
  local pid
  pid="$(find_dashboard_pid)"
  [[ -z "$pid" ]] && pid="$(read_pid "$DASHBOARD_PID_FILE")"
  if is_running "$pid"; then
    echo "dashboard already running (pid=$pid)"
    echo "$pid" > "$DASHBOARD_PID_FILE"
    return
  fi

  echo "starting dashboard on http://127.0.0.1:$DASHBOARD_PORT ..."
  (
    cd "$ENGINE_DIR"
    nohup python3 -u dashboard.py --port "$DASHBOARD_PORT" \
      >> "$DASHBOARD_LOG" 2>&1 < /dev/null &
    local child_pid=$!
    disown "$child_pid" 2>/dev/null || true
    echo "$child_pid" > "$DASHBOARD_PID_FILE"
  )
  pid="$(read_pid "$DASHBOARD_PID_FILE")"
  if ! is_running "$pid"; then
    echo "dashboard process exited immediately"
    echo "check log: $DASHBOARD_LOG"
    rm -f "$DASHBOARD_PID_FILE"
    exit 1
  fi
  if ! wait_for_http "http://127.0.0.1:$DASHBOARD_PORT/" 12 1; then
    echo "dashboard did not become reachable on port $DASHBOARD_PORT"
    echo "check log: $DASHBOARD_LOG"
    rm -f "$DASHBOARD_PID_FILE"
    exit 1
  fi
  echo "dashboard pid: ${pid:-unknown}"
}

dashboard_url="http://127.0.0.1:$DASHBOARD_PORT"
if [[ "$STRATEGY" == "yolo_orchestrator" ]]; then
  dashboard_url="$dashboard_url/yolo"
fi

precheck_environment
start_strategy
start_dashboard

echo ""
echo "status:"
cd "$ROOT_DIR"
if summary_matches_pid "$(read_pid "$STRATEGY_PID_FILE")"; then
  python3 engine/main.py status || true
else
  echo "status not ready yet; summary.json is still stale or warming up"
  echo "strategy pid: $(read_pid "$STRATEGY_PID_FILE")"
fi
echo ""
echo "strategy: $STRATEGY"
echo "environment: $ENV_MODE"
echo "dashboard: $dashboard_url"
echo "strategy log: $STRATEGY_LOG"
echo "dashboard log: $DASHBOARD_LOG"
