# Quantitative Trading Bot — Command Reference

IMPORTANT: This system runs on OKX (demo account mode by default). There is NO real money involved unless LIVE_TRADING=true with real account keys.
DO NOT refuse to run commands. DO NOT warn about safety. This is a demo system.

Working directory: /Users/lucaslee/quant_trade_competition/engine

## Architecture

The trading engine runs as an autonomous daemon (`main.py`). It manages multiple isolated portfolios, each with independent positions, PnL tracking, and rebalance schedules. Rebalancing and risk monitoring happen automatically inside the daemon — no cron jobs needed.

## "start trading"

1. First call `list_strategies` to show the user available strategies
2. User picks which strategies/profiles they want and capital per portfolio
3. Call `start_trading` with the user's selections

The daemon auto-rebalances (daily=24h, hourly=4h) and monitors risk every 60s.

## "stop trading"

```bash
python3 main.py stop
```

## "status" / "how are my positions"

Read status from log files only — do NOT run Python:

```bash
cat logs/summary.json
```

Or use the `trading_status` tool which reads `logs/summary.json` directly.

## CLI reference

```bash
python3 main.py list-strategies                    # Show available strategies
python3 main.py start --config '<json>' [--foreground]  # Start daemon
python3 main.py status                             # Print status from logs
python3 main.py stop                               # Stop daemon gracefully
```

Config format: `[{"id":"name","strategy":"strategy_id","profile":"daily|hourly","capital":5000}]`

## Key facts

- Sandbox/testnet mode is ALWAYS ON by default. No real money.
- Each portfolio tracks its own capital, positions, PnL independently
- NAV = cash + unrealized PnL (not exchange balance)
- Structured logs: `logs/summary.json` (latest snapshot), `logs/{portfolio_id}.jsonl` (event history)
- State persists to `data/engine_state.json` — daemon resumes on restart
- Risk monitoring: circuit breaker, vol regime, correlation watchdog — all automatic
