#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$ROOT_DIR/engine/logs"
CONTROL_DIR="$ROOT_DIR/engine/control"
PORT="${OKX_TRADING_SYSTEM_PORT:-8788}"
URL="http://127.0.0.1:${PORT}/"

mkdir -p "$LOG_DIR" "$CONTROL_DIR"
cd "$ROOT_DIR"

is_running() {
  local pid="${1:-}"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

find_data_refresh_pid() {
  ps -axo pid=,command= | awk '/engine\/data\/refresh_scheduler.py/ && !/awk/ {print $1; exit}'
}

launcher_pid=""
if [[ -f "$CONTROL_DIR/launcher.pid" ]]; then
  launcher_pid="$(tr -d '[:space:]' < "$CONTROL_DIR/launcher.pid" || true)"
fi

if is_running "$launcher_pid"; then
  echo "Restarting launcher: old pid=$launcher_pid"
  kill "$launcher_pid" 2>/dev/null || true
  sleep 1
fi

stamp="$(date +%Y%m%d_%H%M%S)"
log_path="$LOG_DIR/launcher_double_click_${stamp}.log"
echo "Starting OKX trading launcher on ${URL}"
nohup python3 launcher/launcher_server.py --port "$PORT" > "$log_path" 2>&1 < /dev/null &
launcher_pid="$!"
echo "$launcher_pid" > "$CONTROL_DIR/launcher.pid"
sleep 2

data_pid="$(find_data_refresh_pid || true)"
if is_running "$data_pid"; then
  echo "Restarting data refresh: old pid=$data_pid"
  kill "$data_pid" 2>/dev/null || true
  sleep 1
fi

stamp="$(date +%Y%m%d_%H%M%S)"
log_path="$LOG_DIR/data_refresh_double_click_${stamp}.log"
echo "Starting unified data refresh: every 15 minutes"
nohup python3 engine/data/refresh_scheduler.py \
  --interval-sec 900 \
  --max-symbols 30 \
  --timeframes 1h \
  --lookback-days 3 \
  --sleep-sec 0.4 \
  > "$log_path" 2>&1 < /dev/null &
echo "$!" > "$CONTROL_DIR/data_refresh.pid"

echo "Opening ${URL}"
open "$URL"
echo ""
echo "OKX Trading System 已启动。"
echo "前端: ${URL}"
echo "Launcher pid: ${launcher_pid:-unknown}"
echo "Data refresh pid: $(tr -d '[:space:]' < "$CONTROL_DIR/data_refresh.pid" 2>/dev/null || echo unknown)"
echo ""
echo "最近日志："
tail -n 40 "$LOG_DIR"/launcher*.log "$LOG_DIR"/data_refresh*.log 2>/dev/null || true
