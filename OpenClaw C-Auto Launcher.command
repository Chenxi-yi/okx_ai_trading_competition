#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$ROOT_DIR/engine/logs"
CONTROL_DIR="$ROOT_DIR/engine/control"
PORT="${OPENCLAW_LAUNCHER_PORT:-8788}"
URL="http://127.0.0.1:${PORT}/"

mkdir -p "$LOG_DIR" "$CONTROL_DIR"
cd "$ROOT_DIR"

existing_pid=""
if [[ -f "$CONTROL_DIR/launcher.pid" ]]; then
  existing_pid="$(tr -d '[:space:]' < "$CONTROL_DIR/launcher.pid" || true)"
fi

if [[ -n "$existing_pid" ]] && kill -0 "$existing_pid" 2>/dev/null; then
  echo "Launcher already running: pid=$existing_pid"
else
  stamp="$(date +%Y%m%d_%H%M%S)"
  log_path="$LOG_DIR/launcher_double_click_${stamp}.log"
  echo "Starting launcher on ${URL}"
  nohup python3 launcher/launcher_server.py --port "$PORT" > "$log_path" 2>&1 &
  echo $! > "$CONTROL_DIR/launcher.pid"
  sleep 2
fi

echo "Opening ${URL}"
open "$URL"
echo ""
echo "前端已打开。这个窗口可以保留用于查看启动日志，也可以关闭。"
echo "URL: ${URL}"
echo ""
tail -n 40 "$LOG_DIR"/launcher*.log 2>/dev/null || true
