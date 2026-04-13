# Competition Strategies — Master Index

## Active Strategy

| ID | Name | Profile | Signal | Leverage | Status |
|---|---|---|---|---|---|
| `elite_flow` | **Elite Flow** | tick/1min | mOFI + crowding + regime gate | 2x base, 5x cap | ✅ Active |

Capital: 300 USDT seed.

## Status Legend
- ✅ Active — deployed, running on demo

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
