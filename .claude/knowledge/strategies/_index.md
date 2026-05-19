# Competition Strategies — Master Index

Runtime and promotion rules are governed by
`.claude/knowledge/runtime_strategy_governance.md`. A strategy in this index is
not a launcher. It may run only when Strategy Office registers it for an
environment and the environment runner starts it.

## Runtime Status

| ID | Name | Profile | Signal | Leverage | Status |
|---|---|---|---|---|---|
| `elite_flow` | **Elite Flow** | tick/1min | mOFI + crowding + regime gate | 2x base, 5x cap | research/migration needed |
| `monster_coin` | **Monster Coin Catcher** | 5m | volatility/range expansion + cross-sectional strength | research only | 🧪 Research |
| `c_auto_v2` | **C-Auto v2** | 1h | BTC regime + alt cross-sectional ranking | research only | 🧪 Research |
| `trend_pullback_reversal_quality_top20_v1` | **Trend Pullback Quality Top20** | 4h/1h | quality top20 + 5%/1.5% TP/SL | 50U budget, 10U/order, max 3 | live registry, competition only |
| `trend_pullback_reversal_rank_top1_v1` | **Trend Pullback Rank Top1** | 4h/1h | scan-cycle rank top1 + 5%/2% TP/SL | 50U budget, 10U/order, max 3 | live registry, competition only |
| `trend_pullback_reversal_cluster_elite_quality60_v1` | **Trend Pullback Cluster Elite Quality60** | 4h/1h | rolling elite cluster + quality>=0.60 + 5%/0.8% TP/SL | 50U budget, 10U/order, max 3 | live registry, competition only |

Capital: 300 USDT seed.

## Status Legend
- ✅ Active — deployed, running on demo
- 🧪 Research — backtest/paper only, no order placement
- 🧪 Paper Candidate — registered in Strategy Office as paper/candidate, live disabled until explicit start/real confirmation

## CLI Commands
```bash
# List strategies
python3 main.py competition list

# Demo run
python3 main.py session create -s elite_flow
python3 main.py session daemon --foreground

# Session management
python3 main.py session list
python3 main.py session stop-all
```

## Known Issues
- `okx swap close` can fail intermittently — fallback to market flatten is implemented
- WebSocket price cache may fail with API key mismatch on demo — falls back to REST
- Older state files can show stale `running`; actual runtime status must combine
  registry, process, heartbeat freshness, and account reconciliation.
