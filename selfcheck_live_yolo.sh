#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENGINE_DIR="$ROOT_DIR/engine"
LOG_DIR="$ENGINE_DIR/logs"
CHECK_LOG="$LOG_DIR/live_selfcheck.log"
CHECK_JSON="$LOG_DIR/live_selfcheck_status.json"
PORT=8090
TARGET_URL="http://127.0.0.1:${PORT}/api/yolo"

mkdir -p "$LOG_DIR"

timestamp() {
  date +"%Y-%m-%d %H:%M:%S"
}

log() {
  echo "[$(timestamp)] $*" | tee -a "$CHECK_LOG"
}

json_escape() {
  python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'
}

write_status() {
  local status="$1"
  local detail="${2:-}"
  local detail_json
  detail_json="$(printf '%s' "$detail" | json_escape)"
  cat > "$CHECK_JSON" <<EOF
{
  "checked_at": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
  "status": "$status",
  "detail": $detail_json
}
EOF
}

dashboard_ok() {
  curl -sSf "$TARGET_URL" >/dev/null 2>&1
}

strategy_pid() {
  pgrep -f "engine/main.py competition demo-start --strategy yolo_momentum --foreground" | tail -n 1 || true
}

strategy_running() {
  local pid
  pid="$(strategy_pid)"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

validate_yolo_api() {
  local payload
  payload="$(curl -s "$TARGET_URL" 2>/dev/null || true)"
  if [[ -z "$payload" ]]; then
    return 1
  fi
  python3 - "$payload" <<'PY'
import json, sys
try:
    data = json.loads(sys.argv[1])
except Exception:
    raise SystemExit(1)
if data.get("profile") != "live":
    raise SystemExit(2)
if data.get("strategy") != "yolo_momentum":
    raise SystemExit(3)
slots = data.get("slots") or []
if not slots:
    raise SystemExit(4)
slot = slots[0]
if slot.get("state_source") != "yolo_momentum_live_state.json":
    raise SystemExit(5)
print("ok")
PY
}

restart_dashboard() {
  log "dashboard unhealthy; restarting"
  pkill -f "dashboard.py --port ${PORT}" 2>/dev/null || true
  (
    cd "$ENGINE_DIR"
    nohup python3 -u dashboard.py --port "$PORT" >> "$LOG_DIR/dashboard_${PORT}.stdout.log" 2>&1 < /dev/null &
    echo $! > "$ENGINE_DIR/control/dashboard.pid"
  )
  sleep 2
}

restart_strategy() {
  log "strategy unhealthy; restarting live yolo_momentum"
  "$ROOT_DIR/manage_local.sh" stop >> "$CHECK_LOG" 2>&1 || true
  "$ROOT_DIR/start_local.sh" yolo_momentum "$PORT" live >> "$CHECK_LOG" 2>&1
  sleep 3
}

main() {
  log "self-check starting"

  local dashboard_state="ok"
  local strategy_state="ok"

  if ! dashboard_ok || ! validate_yolo_api >/dev/null 2>&1; then
    dashboard_state="bad"
    restart_dashboard
  fi

  if ! strategy_running; then
    strategy_state="bad"
    restart_strategy
  fi

  if ! dashboard_ok || ! validate_yolo_api >/dev/null 2>&1; then
    write_status "error" "dashboard api validation failed after remediation"
    log "self-check failed: dashboard api validation failed after remediation"
    exit 1
  fi

  if ! strategy_running; then
    write_status "error" "yolo_momentum process not running after remediation"
    log "self-check failed: strategy still not running after remediation"
    exit 1
  fi

  write_status "ok" "dashboard=${dashboard_state}, strategy=${strategy_state}"
  log "self-check passed: dashboard=${dashboard_state}, strategy=${strategy_state}"
}

main "$@"
