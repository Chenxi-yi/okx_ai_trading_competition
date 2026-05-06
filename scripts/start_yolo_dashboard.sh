#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENGINE_DIR="$ROOT/engine"
CONTROL_DIR="$ENGINE_DIR/control"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
export PYTHONUNBUFFERED=1

mkdir -p "$CONTROL_DIR"
rm -f "$CONTROL_DIR/dashboard.pid"

cd "$ENGINE_DIR"
exec python3 -u dashboard.py --port 8090
