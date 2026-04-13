# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project
Quantitative crypto trading engine for OKX's internal AI Skills Challenge.
Seed capital: 300 USDT (organiser incentive). Top up dynamically — see capital policy. Window: April 9–23 2026 (14 days). Engine: `engine/`. Python.
Goal: maximise ROI. All orders via Agent Trade Kit CLI — never raw ccxt.

## Competition Rules (Non-Negotiable)
- Winning metric: `ROI = AI_net_pnl ÷ (initial_nav + cumulative_deposits) × 100%`
- Orders MUST go via `okx swap place` CLI — tag `agentTradeKit` auto-injected, cannot be faked
- Only `*-USDT-SWAP` perpetuals count toward P&L
- No manual trading. No self-dealing. No depositing more capital (dilutes ROI denominator).
- Skills file due April 24 23:59 — late = disqualified
- Leaderboard refreshes hourly, includes unrealised PnL

## Common Commands

All Python commands run from project root. Working directory: `/opt/quant_trade_competition`.

```bash
# Install dependencies
pip install -r engine/requirements.txt

# Strategy listing
python3 engine/main.py list-strategies
python3 engine/main.py competition list

# Backtest a single strategy
python3 engine/main.py competition backtest --strategy <id>
python3 engine/main.py competition backtest --strategy <id> --start 2025-01-01 --end 2026-03-31

# Backtest all strategies + compare
python3 engine/main.py competition backtest-all
python3 engine/main.py competition compare

# Demo run (daemon)
python3 engine/main.py competition demo-start --strategy <id>
python3 engine/main.py competition demo-status

# Daemon management
python3 engine/main.py start --config '<json>'
python3 engine/main.py status
python3 engine/main.py stop

# Tools (run from project root)
python3 .claude/tools/trading_status.py          # engine health check
python3 .claude/tools/check_balance.py            # OKX account balance
python3 .claude/tools/check_positions.py          # open swap positions
python3 .claude/tools/backtest_strategy.py <id>   # run backtest
python3 .claude/tools/place_order.py --instId BTC-USDT-SWAP --side buy --sz 1 --dry-run
python3 .claude/tools/start_demo.py <id>          # start demo run
python3 .claude/tools/stop_engine.py              # stop daemon
python3 .claude/tools/log_error.py --code X --msg "..."  # error logging
```

There are no unit tests or linters configured in this repository.

## Architecture

```
/opt/quant_trade_competition/
  CLAUDE.md                          ← this file
  index.ts                           MCP tool server (TypeScript, exposes engine CLI to Claude)
  engine/                            All Python code lives here
    main.py                          Unified CLI entry point (list-strategies, start, stop, status, competition *)
    engine/trading_engine.py         TradingEngine — daemon run loop, portfolio management
    session.py                       Multi-session trading engine for concurrent strategies
    config/
      settings.py                    Global config, OKX credentials (env → TOML fallback), BASE_DIR
      profiles.py                    DAILY_PROFILE / HOURLY_PROFILE dicts, get_profile()
      competition_strategies.json    Named competition strategy registry (single source of truth)
      strategies.json                Base strategy/preset registry
    competition/
      registry.py                    CompetitionRegistry — loads competition_strategies.json
      backtester.py                  CompetitionBacktester — run/compare backtests
      compare.py                     compare_demo(), compare_backtest(), print_leaderboard()
      strategies/
        elite_flow.py                Elite Flow — multi-level OFI + crowding + regime gate (standalone async, sole competition strategy)
    backtest/
      runner.py                      BacktestRunner(profile_name, profile_overrides, risk_overrides)
      metrics.py                     compute_all_metrics(), print_metrics()
    execution/
      broker.py                      Broker class — market_buy/sell, get_balance, get_positions
    data/
      fetcher.py                     fetch_ohlcv(), fetch_universe() — OKX via ccxt, Parquet cache
      feed_ws.py                     WebSocketPriceCache — live price stream
    risk/
      risk_manager_v2.py             RiskManagerV2 — ATR stops, circuit breaker, vol regime, correlation watchdog
    logging_/
      structured_logger.py           StructuredLogger — competition JSON logging
    logs/                            Runtime logs (summary.json, heartbeat.json, per-portfolio JSONL)
    dashboard.py                     Web dashboard for monitoring
  deploy/
    setup.sh                         Server deployment script
    trading-engine.service            systemd unit for engine daemon
    trading-dashboard.service         systemd unit for dashboard
    nginx.conf                       Reverse proxy config
  .claude/
    tools/                           Claude agent tools (see _registry.md for full docs)
    knowledge/                       Domain knowledge base (strategies, API, risk, competition rules)
    errors/registry.jsonl            Structured error log (self-updating)
```

### Key data flows

**Elite Flow pipeline:** `elite_flow.py` runs its own async event loop with WebSocket streams (orderbook, trades, 1m OHLCV). Signals via multi-level OFI + crowding + regime gate → conviction state machine (FLAT/PROBE/FULL) → orders via `okx swap place` CLI directly. All orders tagged `agentTradeKit` for competition scoring.

**Session daemon:** `session.py` SessionDaemon manages strategy lifecycle. Creates sessions via `main.py session create -s elite_flow`, runs via `main.py session daemon --foreground`. Writes `logs/summary.json` (every 15s) and `logs/nav_history.jsonl` for dashboard.

**Dashboard:** `dashboard.py` serves live NAV chart, positions, and bills (fetched from OKX fills API). All times in SGT.

## Knowledge Base
Read before acting. Never assume — look it up first.

| File | When to read |
|---|---|
| `.claude/knowledge/strategies/_index.md` | Strategy comparison, status, backtest results |
| `.claude/knowledge/strategies/<id>.md` | Full signal formulas, parameters, code pointers for one strategy |
| `.claude/knowledge/okx_api.md` | Before any CLI call — instId, sz, profiles, pitfalls |
| `.claude/knowledge/engine_architecture.md` | Before modifying engine code or running commands |
| `.claude/knowledge/competition.md` | Scoring rules, eligibility, deadlines |
| `.claude/knowledge/risk_system.md` | Risk thresholds, circuit breakers |

## Tools
**Full reference:** `.claude/tools/_registry.md` — args, return format, examples, permissions.

Quick picks: `trading_status.py` · `check_balance.py` · `check_positions.py` · `backtest_strategy.py` · `place_order.py` · `start_demo.py` · `stop_engine.py` · `log_error.py`

## Agent Roles & Permissions
**Analyst** — Read KB, run backtests, read logs. No order placement, no code edits.

**Trader** — Place/close orders (demo by default). Must run `check_positions.py` before placing. Read `okx_api.md` first. Never `--profile live` without explicit user instruction.

**Engineer** — Debug code, update KB files, update tools. No live orders. Must `log_error.py` after every fix.

**Commander** — Orchestrates sub-agents. All read tools + `start_demo.py`. User confirmation required for: `stop_engine.py`, `--profile live`, any KB deletion.

When forming teams: state role + allowed tools + objective in agent prompt. Sub-agents cannot exceed role permissions.

## Self-Update Protocol
Every error encountered → `python3 .claude/tools/log_error.py --code X --msg "..." --context "{...}"`
Every error resolved → same command with `--context '{"resolved": true, "fix": "..."}'`
KB found stale or wrong → edit the relevant `.claude/knowledge/` file directly
New API behaviour discovered → update `.claude/knowledge/okx_api.md`
After backtest → update results table in `.claude/knowledge/strategies/<id>.md`
Goal: zero repeated errors. Every session builds on the last.

## Critical Constraints
1. **sz = contracts, not coins.** BTC: 1 contract = 0.01 BTC. ETH: 0.1 ETH. See `okx_api.md`.
2. **Position mode must be `net`.** Set once: `okx --profile demo account set-position-mode net`
3. **Correlation watchdog is expected.** ρ > 0.85 across 6 assets fires every rebalance → 0.60x scalar. Not a bug.
4. **Demo keys only.** All non-competition runs use `--profile demo`. Live requires user authorisation.
5. **Dry-run before placing.** Always `place_order.py ... --dry-run` first.
6. **Check engine state first.** Run `trading_status.py` at the start of every session.
7. **Credentials in `~/.okx/config.toml`.** Priority: env vars → TOML. No keychain.
