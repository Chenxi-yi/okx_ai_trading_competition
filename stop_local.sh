#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONTROL_DIR="$ROOT_DIR/engine/control"

stop_pattern() {
  local name="$1"
  local pattern="$2"
  local pids
  pids="$(pgrep -f "$pattern" || true)"
  if [[ -z "$pids" ]]; then
    return
  fi

  echo "stopping $name via process scan ($pids)"
  kill $pids 2>/dev/null || true
}

stop_pid_file() {
  local name="$1"
  local file="$2"
  if [[ ! -f "$file" ]]; then
    echo "$name: no pid file"
    return
  fi

  local pid
  pid="$(tr -d '[:space:]' < "$file")"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    echo "stopping $name (pid=$pid)"
    kill "$pid"
  else
    echo "$name: stale pid file ($pid)"
  fi
  rm -f "$file"
}

stop_pattern "dashboard" "python3 dashboard.py --port"
stop_pattern "elite_flow" "engine/main.py competition demo-start --strategy elite_flow --foreground"
stop_pattern "yolo_momentum" "engine/main.py competition demo-start --strategy yolo_momentum --foreground"
stop_pattern "yolo_orchestrator" "engine/main.py competition demo-start --strategy yolo_orchestrator --foreground"

stop_pid_file "dashboard" "$CONTROL_DIR/dashboard.pid"
stop_pid_file "elite_flow" "$CONTROL_DIR/elite_flow.pid"
stop_pid_file "yolo_momentum" "$CONTROL_DIR/yolo_momentum.pid"
stop_pid_file "yolo_orchestrator" "$CONTROL_DIR/yolo_orchestrator.pid"
