#!/usr/bin/env zsh
set -euo pipefail

cd /Users/yichenxi/Desktop/okx_ai_skill_challenage
mkdir -p engine/logs engine/data/training_history

exec /usr/bin/env PYTHONPYCACHEPREFIX=/tmp/okx_pycache python3 scripts/fetch_training_history.py \
  --run-id train_hist_vol5m_1h_20240101_20260424 \
  --min-volume-usd 5000000 \
  --max-symbols 300 \
  --start 2024-01-01 \
  --end 2026-04-24 \
  --timeframes 1h \
  --sleep-sec 4 \
  --retry-attempts 8 \
  --retry-sleep-sec 20 \
  --min-rows 100
