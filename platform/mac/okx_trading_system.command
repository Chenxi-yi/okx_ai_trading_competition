#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
LOG_DIR="$ROOT_DIR/engine/logs"
CONTROL_DIR="$ROOT_DIR/engine/control"
PORT="${OKX_TRADING_SYSTEM_PORT:-8788}"
URL="http://127.0.0.1:${PORT}/"
PYTHON_BIN="${OKX_TRADING_SYSTEM_PYTHON:-/Library/Developer/CommandLineTools/usr/bin/python3}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="$(command -v python3)"
fi

PYTHON_USER_SITE="$("$PYTHON_BIN" -m site --user-site 2>/dev/null || true)"
if [[ -n "$PYTHON_USER_SITE" ]]; then
  export PYTHONPATH="${PYTHON_USER_SITE}${PYTHONPATH:+:$PYTHONPATH}"
fi
export OKX_TRADING_SYSTEM_PYTHON="$PYTHON_BIN"

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
nohup "$PYTHON_BIN" launcher/launcher_server.py --port "$PORT" > "$log_path" 2>&1 < /dev/null &
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
nohup "$PYTHON_BIN" engine/data/refresh_scheduler.py \
  --interval-sec 900 \
  --max-symbols 150 \
  --extra-symbols XAU/USDT \
  --timeframes 5m,15m,1h,4h,1d \
  --lookback-days 3 \
  --sleep-sec 0.2 \
  --derivatives-max-symbols 150 \
  --derivatives-run-id c_auto_live_derivatives_5m \
  --derivatives-kinds funding,open_interest,long_short \
  --derivatives-timeframe 5m \
  --derivatives-lookback-days 3 \
  > "$log_path" 2>&1 < /dev/null &
echo "$!" > "$CONTROL_DIR/data_refresh.pid"

echo "Opening ${URL}"
open "$URL"
echo ""
echo "OKX Trading System started."
echo "Frontend: ${URL}"
echo "Python: ${PYTHON_BIN}"
echo "Launcher pid: ${launcher_pid:-unknown}"
echo "Data refresh pid: $(tr -d '[:space:]' < "$CONTROL_DIR/data_refresh.pid" 2>/dev/null || echo unknown)"
echo ""
echo "Recent logs:"
tail -n 40 "$LOG_DIR"/launcher*.log "$LOG_DIR"/data_refresh*.log 2>/dev/null || true
