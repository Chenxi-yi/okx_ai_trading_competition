# Tool Registry

All tools live in `.claude/tools/`. Run from project root: `python3 .claude/tools/<tool>.py`

Default `--profile` is always `demo` unless stated. Never use `--profile live` without explicit user authorisation.

---

## trading_status.py
**Purpose:** Read engine health and portfolio P&L from logs. No API call — reads `engine/logs/summary.json`.

**Permission:** All agents

```bash
python3 .claude/tools/trading_status.py
python3 .claude/tools/trading_status.py --json   # raw JSON output
```

**Returns:** Engine status (RUNNING/STOPPED), per-portfolio NAV / PnL / drawdown / risk regime, total NAV.

**Exit:** 0 = engine running, 1 = not running or no data.

---

## check_balance.py
**Purpose:** Fetch live account balance from OKX via ATK CLI.

**Permission:** All agents

```bash
python3 .claude/tools/check_balance.py
python3 .claude/tools/check_balance.py --profile live
python3 .claude/tools/check_balance.py --json
```

**Returns:** Total equity (USDT), available balance, USDT balance, unrealised PnL, utilisation %.

**Note:** OKX returns `""` for unused numeric fields — handled internally. See error `OKX_EMPTY_STRING_FIELDS`.

---

## check_positions.py
**Purpose:** Fetch open swap positions from OKX via ATK CLI.

**Permission:** All agents

```bash
python3 .claude/tools/check_positions.py
python3 .claude/tools/check_positions.py --profile live
python3 .claude/tools/check_positions.py --json
```

**Returns:** Table of open swaps: instId, side, size (contracts), avgPx, markPx, unrealised PnL.

---

## backtest_strategy.py
**Purpose:** Run a full competition strategy backtest using `CompetitionBacktester`. Saves results to `engine/logs/competition/<id>/backtest_latest.json`.

**Permission:** Analyst, Engineer

```bash
python3 .claude/tools/backtest_strategy.py elite_flow
python3 .claude/tools/backtest_strategy.py elite_flow --start 2025-01-01 --end 2026-03-31
python3 .claude/tools/backtest_strategy.py elite_flow --no-cache   # force re-fetch data
```

**Arguments:**
| Arg | Type | Default | Description |
|---|---|---|---|
| `strategy_id` | str | required | ID from `competition_strategies.json` |
| `--start` | date | `2025-01-01` | Backtest start |
| `--end` | date | `2026-03-31` | Backtest end |
| `--no-cache` | flag | False | Force OKX data re-fetch |

**Returns:** Metrics table (return, Sharpe, max DD, win rate, trades, fees) + monthly P&L breakdown.

**Note:** Requires ~90–150s for daily data. Use cached data unless cache is stale (checked automatically).

---

## place_order.py
**Purpose:** Place a perpetual swap order via OKX Agent Trade Kit CLI. Tag `agentTradeKit` injected automatically.

**Permission:** Trader, Commander

```bash
# Dry run first — always
python3 .claude/tools/place_order.py --instId BTC-USDT-SWAP --side buy --sz 1 --dry-run

# Execute (demo)
python3 .claude/tools/place_order.py --instId BTC-USDT-SWAP --side buy --sz 1

# With TP/SL
python3 .claude/tools/place_order.py --instId ETH-USDT-SWAP --side sell --sz 5 --sl 2300 --tp 1900

# Live — requires explicit flag + user authorisation
python3 .claude/tools/place_order.py --instId BTC-USDT-SWAP --side buy --sz 1 --profile live
```

**Arguments:**
| Arg | Type | Default | Description |
|---|---|---|---|
| `--instId` | str | required | Must end in `-USDT-SWAP` |
| `--side` | buy/sell | required | Direction |
| `--sz` | int | required | Contracts (NOT coins). BTC: 1 contract = 0.01 BTC |
| `--posSide` | str | `net` | Use `net` for net position mode (default) |
| `--ordType` | str | `market` | market / limit / post_only |
| `--px` | float | None | Limit price (required for limit orders) |
| `--tdMode` | str | `cross` | cross / isolated |
| `--tp` | float | None | Take-profit trigger price |
| `--sl` | float | None | Stop-loss trigger price |
| `--profile` | str | `demo` | demo or live |
| `--dry-run` | flag | False | Print command, do not execute |

**Returns:** JSON `{"success": true, "data": {"ordId": "...", ...}}` or `{"success": false, "error": "..."}`.

**Critical:** Always `--dry-run` first. sz is contracts not coins. See `SZ_UNIT_MISMATCH` in error registry.

---

## start_demo.py
**Purpose:** Start a competition strategy demo run (launches engine daemon).

**Permission:** Commander

```bash
python3 .claude/tools/start_demo.py elite_flow
python3 .claude/tools/start_demo.py elite_flow --foreground   # don't daemonize
```

**Arguments:**
| Arg | Type | Description |
|---|---|---|
| `strategy_id` | str | Strategy ID (validated against registry) |
| `--foreground` | flag | Run in foreground instead of daemonizing |

**Returns:** Confirmation with strategy name + capital. Fails if engine already running (shows existing PID).

---

## stop_engine.py
**Purpose:** Gracefully stop the trading daemon. Prints final NAV snapshot before stopping.

**Permission:** Commander + user confirmation required

```bash
python3 .claude/tools/stop_engine.py
python3 .claude/tools/stop_engine.py --force   # SIGKILL if graceful stop fails after 15s
```

**Returns:** Final NAV/PnL snapshot, confirmation of stop. Exit 1 if no daemon running.

---

## log_error.py
**Purpose:** Append a structured error record to `.claude/errors/registry.jsonl`. **Run after every error encountered or resolved.** This is the self-update tool.

**Permission:** All agents

```bash
# Log a new error
python3 .claude/tools/log_error.py --code ATK_NOT_FOUND --msg "okx CLI not found on PATH"

# Log with context
python3 .claude/tools/log_error.py --code NET_MODE_REQUIRED \
  --msg "posSide error on swap place" \
  --context '{"instId": "BTC-USDT-SWAP", "profile": "demo"}'

# Mark resolved
python3 .claude/tools/log_error.py --code NET_MODE_REQUIRED \
  --msg "RESOLVED" \
  --context '{"resolved": true, "fix": "run set-position-mode net first"}'
```

**Arguments:**
| Arg | Type | Description |
|---|---|---|
| `--code` | str | UPPER_SNAKE_CASE unique error code |
| `--msg` | str | Human-readable description |
| `--context` | JSON str | Optional dict with details or `{"resolved": true, "fix": "..."}` |

**Returns:** `[LOGGED] CODE` or `[RESOLVED] CODE`. Always exits 0.

---

## Quick Decision Guide

| I want to... | Use |
|---|---|
| Check if engine is running | `trading_status.py` |
| Check account balance | `check_balance.py` |
| See open positions | `check_positions.py` |
| Run a backtest | `backtest_strategy.py <id>` |
| Place an order (test first!) | `place_order.py ... --dry-run` then without |
| Start a strategy demo | `start_demo.py <id>` |
| Stop the engine | `stop_engine.py` |
| Record an error or fix | `log_error.py --code X --msg Y` |
