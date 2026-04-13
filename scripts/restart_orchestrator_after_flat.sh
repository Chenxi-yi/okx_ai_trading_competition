#!/bin/zsh
set -euo pipefail

ROOT="/Users/yichenxi/.openclaw/workspace/okx_ai_skill_challenage"
LOG="$ROOT/engine/logs/restart_orchestrator_after_flat.log"

cd "$ROOT"

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] watcher started" >> "$LOG"

while true; do
  positions="$(okx --profile live --json account positions 2>>"$LOG" || true)"
  if [[ "$positions" == "[]" ]]; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] account flat, restarting orchestrator" >> "$LOG"
    ./stable_yolo.sh orchestrator restart >> "$LOG" 2>&1 || true
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] restart done, watcher exiting" >> "$LOG"
    exit 0
  fi
  sleep 10
done
