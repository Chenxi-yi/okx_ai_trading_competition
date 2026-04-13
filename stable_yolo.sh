#!/bin/zsh
set -euo pipefail

ROOT="/Users/yichenxi/.openclaw/workspace/okx_ai_skill_challenage"
MODE="${1:-momentum}"
if [[ "$MODE" != "momentum" && "$MODE" != "orchestrator" ]]; then
  MODE="momentum"
  COMMAND="${1:-status}"
else
  COMMAND="${2:-status}"
fi
if [[ "$MODE" == "orchestrator" ]]; then
  BOT_LABEL="ai.openclaw.okx-ai-yolo-orchestrator-live"
else
  BOT_LABEL="ai.openclaw.okx-ai-yolo-live"
fi
DASHBOARD_LABEL="ai.openclaw.okx-ai-yolo-dashboard"
BOT_PLIST_SRC="$ROOT/launchd/$BOT_LABEL.plist"
DASHBOARD_PLIST_SRC="$ROOT/launchd/$DASHBOARD_LABEL.plist"
BOT_PLIST_DST="$HOME/Library/LaunchAgents/$BOT_LABEL.plist"
DASHBOARD_PLIST_DST="$HOME/Library/LaunchAgents/$DASHBOARD_LABEL.plist"
DASHBOARD_URL="http://127.0.0.1:8090/yolo"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

usage() {
  cat <<'EOF'
Usage:
  ./stable_yolo.sh <command>
  ./stable_yolo.sh momentum <command>
  ./stable_yolo.sh orchestrator <command>

Commands:
  install   Install or refresh launchd agents
  start     Start or restart live yolo + dashboard
  stop      Stop live yolo + dashboard
  restart   Restart live yolo + dashboard
  status    Show launchd and API status
  logs      Tail launchd logs
EOF
}

is_loaded() {
  local label="$1"
  launchctl print "gui/$UID/$label" >/dev/null 2>&1
}

bootstrap_one() {
  local label="$1"
  local plist="$2"
  if ! is_loaded "$label"; then
    launchctl bootstrap "gui/$UID" "$plist"
  fi
}

kickstart_one() {
  local label="$1"
  launchctl kickstart -k "gui/$UID/$label"
}

bootout_one() {
  local label="$1"
  if is_loaded "$label"; then
    launchctl bootout "gui/$UID/$label"
  fi
}

install_agents() {
  mkdir -p "$HOME/Library/LaunchAgents"
  cp "$BOT_PLIST_SRC" "$BOT_PLIST_DST"
  cp "$DASHBOARD_PLIST_SRC" "$DASHBOARD_PLIST_DST"
  bootstrap_one "$BOT_LABEL" "$BOT_PLIST_DST"
  bootstrap_one "$DASHBOARD_LABEL" "$DASHBOARD_PLIST_DST"
  echo "Installed launchd agents."
}

start_agents() {
  install_agents
  kickstart_one "$BOT_LABEL"
  kickstart_one "$DASHBOARD_LABEL"
  echo "Started: $BOT_LABEL, $DASHBOARD_LABEL"
  echo "Mode: $MODE"
  echo "Dashboard: $DASHBOARD_URL"
}

show_status() {
  echo "mode: $MODE"
  echo "launchd:"
  if is_loaded "$BOT_LABEL"; then
    echo "bot: loaded"
  else
    echo "bot: not loaded"
  fi
  if is_loaded "$DASHBOARD_LABEL"; then
    echo "dashboard: loaded"
  else
    echo "dashboard: not loaded"
  fi
  echo
  echo "api:"
  curl -fsS "$DASHBOARD_URL" >/dev/null && echo "dashboard page: ok ($DASHBOARD_URL)" || echo "dashboard page: unavailable"
  curl -fsS "http://127.0.0.1:8090/api/yolo" || true
}

tail_logs() {
  local bot_out="$ROOT/engine/logs/launchd-yolo-live.out.log"
  local bot_err="$ROOT/engine/logs/launchd-yolo-live.err.log"
  if [[ "$MODE" == "orchestrator" ]]; then
    bot_out="$ROOT/engine/logs/launchd-yolo-orchestrator-live.out.log"
    bot_err="$ROOT/engine/logs/launchd-yolo-orchestrator-live.err.log"
  fi
  tail -n 40 -f \
    "$bot_out" \
    "$bot_err" \
    "$ROOT/engine/logs/launchd-yolo-dashboard.out.log" \
    "$ROOT/engine/logs/launchd-yolo-dashboard.err.log"
}

case "$COMMAND" in
  install)
    install_agents
    ;;
  start)
    start_agents
    ;;
  stop)
    bootout_one "$BOT_LABEL"
    bootout_one "$DASHBOARD_LABEL"
    echo "Stopped launchd agents."
    ;;
  restart)
    start_agents
    ;;
  status)
    show_status
    ;;
  logs)
    tail_logs
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage
    exit 1
    ;;
esac
