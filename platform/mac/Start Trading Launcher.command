#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
PORT="${LAUNCHER_PORT:-8788}"

cd "$ROOT_DIR"
mkdir -p engine/control engine/logs

if ! curl -s "http://127.0.0.1:${PORT}/api/status" >/dev/null 2>&1; then
  nohup python3 launcher/launcher_server.py --port "$PORT" \
    > engine/logs/launcher_server.out 2>&1 < /dev/null &
  echo "$!" > engine/control/launcher.pid
  sleep 1
fi

open "http://127.0.0.1:${PORT}/"
