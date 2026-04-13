#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
START_SCRIPT="$ROOT_DIR/start_local.sh"
STOP_SCRIPT="$ROOT_DIR/stop_local.sh"
DEFAULT_PORT="8080"

print_usage() {
  cat <<'EOF'
usage:
  ./manage_local.sh
  ./manage_local.sh start [elite_flow|yolo_momentum|yolo_orchestrator] [port] [demo|live]
  ./manage_local.sh stop
  ./manage_local.sh status

interactive mode:
  1. choose start / stop / status
  2. if start, choose environment
  3. choose strategy
  4. script runs precheck, then launches strategy + dashboard together

codex note:
  for reliable OKX access from Codex, ask Codex to run
  "./manage_local.sh start ..." outside the sandbox
EOF
}

run_status() {
  (
    cd "$ROOT_DIR"
    python3 engine/main.py status || true
  )
}

prompt_action() {
  echo "提示：如果你是在 Codex 对话里启动，应该让 Codex 用非沙盒权限执行启动命令。"
  echo "选择操作："
  echo "  1) 一键启动"
  echo "  2) 一键暂停"
  echo "  3) 查看状态"
  echo "  4) 退出"
  read "choice?请输入 1/2/3/4: "
  echo "${choice:-4}"
}

prompt_environment() {
  echo ""
  echo "选择环境："
  echo "  1) demo"
  echo "  2) production (live)"
  read "choice?请输入 1/2: "

  case "${choice:-1}" in
    1) echo "demo" ;;
    2) echo "live" ;;
    *)
      echo "invalid environment choice: ${choice:-}"
      exit 1
      ;;
  esac
}

prompt_strategy() {
  echo ""
  echo "选择策略："
  echo "  1) elite_flow"
  echo "  2) yolo_momentum"
  echo "  3) yolo_orchestrator"
  read "choice?请输入 1/2/3: "

  case "${choice:-1}" in
    1) echo "elite_flow" ;;
    2) echo "yolo_momentum" ;;
    3) echo "yolo_orchestrator" ;;
    *)
      echo "invalid strategy choice: ${choice:-}"
      exit 1
      ;;
  esac
}

prompt_port() {
  read "port?dashboard 端口（默认 ${DEFAULT_PORT}）: "
  echo "${port:-$DEFAULT_PORT}"
}

start_flow() {
  local strategy="$1"
  local port="$2"
  local env_mode="$3"
  "$START_SCRIPT" "$strategy" "$port" "$env_mode"
}

stop_flow() {
  "$STOP_SCRIPT"
}

action="${1:-interactive}"

case "$action" in
  interactive)
    choice="$(prompt_action)"
    case "$choice" in
      1)
        env_mode="$(prompt_environment)"
        strategy="$(prompt_strategy)"
        port="$(prompt_port)"
        start_flow "$strategy" "$port" "$env_mode"
        ;;
      2)
        stop_flow
        ;;
      3)
        run_status
        ;;
      4)
        echo "退出"
        ;;
      *)
        echo "invalid action: $choice"
        exit 1
        ;;
    esac
    ;;
  start)
    strategy="${2:-elite_flow}"
    port="${3:-$DEFAULT_PORT}"
    env_mode="${4:-demo}"
    start_flow "$strategy" "$port" "$env_mode"
    ;;
  stop)
    stop_flow
    ;;
  status)
    run_status
    ;;
  -h|--help|help)
    print_usage
    ;;
  *)
    echo "unknown action: $action"
    print_usage
    exit 1
    ;;
esac
