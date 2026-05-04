# Professional Quant System Module Map

Last updated: 2026-05-04T00:24:10+0800

This is the complete module checklist for the personal OKX quant system. Every
future implementation must map to one of these modules. If a feature does not
fit, update this map before writing code.

## Top-Level Architecture

```text
0. Contracts & Schemas
1. Data Foundation
2. Feature & Label Store
3. Strategy Office
4. Research Lab
5. Backtest & Simulation
6. Portfolio Construction
7. Position Management
8. Risk Management
9. Execution Management
10. Portfolio Accounting & Reconciliation
11. Runtime Orchestration
12. Observability & Control Tower
```

## 0. Contracts & Schemas

Purpose: define the objects shared by all environments.

Responsibilities:
- `MarketState`, `Signal`, `Decision`, `OrderIntent`, `Fill`
- `Position`, `PortfolioState`
- `StrategySpec`, `StrategyContext`, Strategy Protocol
- frozen dataclasses where possible

Current status: partial implementation.

Implemented:
- `engine/contracts/`

Missing:
- explicit `PositionIntent` / `TargetPosition` contract
- explicit `ExitIntent` contract
- dataset/version references on all runtime events

## 1. Data Foundation

Purpose: own raw market/account data, coverage, freshness, and quality.

Responsibilities:
- historical OHLCV download/cache
- live market snapshots
- instrument metadata: ctVal, lotSz, minSz, tickSz, leverage caps
- funding, OI, long-short ratio, orderbook/trades
- dataset catalog and coverage tracking
- point-in-time universe membership

Current status: partial implementation.

Implemented:
- `engine/data/fetcher.py`
- `engine/data/quality.py`
- `engine/data/catalog.py`
- `engine/data/instruments.py`
- `engine/data/universe.py`
- `engine/download/`
- `scripts/data_download_server.py`

Missing:
- full data warehouse layout
- point-in-time universe history
- derivatives structure download control in the new dashboard
- row-level gap/staleness audits beyond downloader progress summaries

## 2. Feature & Label Store

Purpose: make research and live features share code and metadata.

Responsibilities:
- point-in-time feature builders
- label builders separated from strategy code
- feature registry
- label registry
- feature validation and IC summaries

Current status: partial implementation.

Implemented:
- `engine/features/`
- `engine/research/feature_pipeline.py`

Missing:
- online/live feature adapter
- feature version pinning in runtime journals
- richer leakage tests
- feature parity checks between research and live

## 3. Strategy Office

Purpose: source of truth for strategy identity and lifecycle.

Responsibilities:
- strategy IDs
- parameter set IDs
- book classification: core / tactical / speculative
- status: idea / research / backtest / paper / live / paused / retired
- performance records
- promotion records
- live allocation gates

Current status: partial implementation.

Implemented:
- `engine/registry/`
- `engine/config/strategy_registry.json`
- `engine/registry_cli.py`
- `engine/strategies/specs/`

Missing:
- parameter variant creation CLI
- registry enforcement inside runtime loaders
- strategy owner/review checklist automation

## 4. Research Lab

Purpose: run experiments without touching live trading.

Responsibilities:
- experiments
- walk-forward
- feature/label studies
- parameter sweeps
- Monte Carlo/path stress
- reports
- attach evidence back to Strategy Office

Current status: partial implementation.

Implemented:
- `engine/research/`
- `engine/research/c_auto_experiment.py`

Missing:
- batch experiment runner
- experiment comparison reports
- Monte Carlo integration for path-dependent strategies
- automated performance registration policy

## 5. Backtest & Simulation

Purpose: replay strategy, portfolio, risk, execution, and accounting through the
same path used by paper/live.

Responsibilities:
- historical event loop
- delayed execution model
- mark/execution price separation
- fees, slippage, funding
- tradeability gates
- decision journal output
- result artifacts

Current status: partial implementation.

Implemented:
- legacy `engine/backtest/engine.py`
- new `engine/backtest/pro_engine.py`
- delayed execution in new engine
- formal artifacts: nav, fills, attribution, summary, manifest

Missing:
- next-bar execution enforcement
- funding cashflow integration in new engine
- exits/time stops in the historical loop
- attribution output
- formal result writer

## 6. Portfolio Construction

Purpose: choose which strategy signals should become desired portfolio exposure.

Responsibilities:
- signal arbitration
- multi-strategy conflict resolution
- capital allocation by book
- strategy weighting
- symbol exposure planning
- Kelly/EV sizing before risk caps

Current status: early implementation.

Implemented:
- `engine/arbitration/portfolio_arbiter.py`

Missing:
- book-level capital allocator
- multi-strategy budget allocator
- correlation-aware allocation
- long/short netting policy
- allocation decay after underperformance

## 7. Position Management

Purpose: own the lifecycle of open positions. This is separate from risk and
execution.

Responsibilities:
- decide open / add / reduce / close / reverse / hold
- prevent duplicate entries from repeated signals
- merge or isolate positions by strategy and symbol
- maintain target position and current position gap
- enforce time stop, signal expiry, target, stop, partial exits
- translate `Decision` into position-change intents
- position ownership and attribution metadata

Current status: early implementation.

Existing pieces:
- `engine/contracts/portfolio.py`
- `engine/accounting/portfolio_accounting.py`
- `engine/arbitration/portfolio_arbiter.py`
- `engine/position/position_manager.py`

Required directory:
- `engine/position/`

Implemented contracts/classes:
- `PositionIntent`
- `PositionManager`
- target/stop/time-stop exit decision generation
- same-symbol conflict policy: `winner_takes_symbol`
- partial target exit policy

Still required:
- richer `ExitPolicy`
- `PositionLifecycle`
- richer `PositionConflictPolicy`

This module must sit between Portfolio Construction and Risk:

```text
Signal -> PortfolioConstruction -> PositionManager -> Risk -> Execution
```

It must not be hidden inside Risk or Execution.

## 8. Risk Management

Purpose: reduce or reject position changes that violate limits.

Responsibilities:
- instrument risk
- strategy risk
- account risk
- correlation/crowding risk
- data stale gates
- drawdown/daily loss gates
- kill switch

Current status: partial implementation.

Implemented:
- `engine/risk/account.py`
- legacy `engine/risk/risk_manager_v2.py`
- `engine/risk/instrument.py`
- `engine/risk/strategy.py`
- `engine/risk/kill_switch.py`

Missing:
- reduce-only mode after reconciliation mismatch
- risk event aggregation

## 9. Execution Management

Purpose: turn approved order intents into backtest/paper/live fills.

Responsibilities:
- contract sizing
- order type selection
- OKX CLI live route
- backtest fill simulation
- paper fill simulation
- retry/error handling
- reduce-only handling

Current status: partial implementation.

Implemented:
- `engine/execution/router.py`
- `engine/execution/broker.py`
- `engine/execution/reconciliation.py`
- `engine/kit/`
- `scripts/kit_supervisor.py`

Missing:
- order state machine
- richer retry/idempotency policy
- cancel/replace
- reduce-only close flows
- live fill reconciliation by order ID

## 10. Portfolio Accounting & Reconciliation

Purpose: convert fills and mark prices into trustworthy account state.

Responsibilities:
- positions
- average entry
- realized/unrealized PnL
- fees
- funding
- NAV
- margin/leverage approximation
- reconcile local state against OKX live account

Current status: partial implementation.

Implemented:
- `engine/accounting/portfolio_accounting.py`

Missing:
- funding application
- realized PnL edge cases for reversal
- margin and liquidation estimates
- OKX account reconciliation
- drift detection and reduce-only fail mode

## 11. Runtime Orchestration

Purpose: run the system reliably.

Responsibilities:
- scheduler
- session lifecycle
- paper/live runners
- heartbeat
- crash recovery
- lock files / pid files
- environment selection

Current status: partial implementation.

Implemented:
- `engine/runtime/pipeline.py`
- `engine/runtime/paper_runner.py`
- `engine/runtime/paper_scheduler.py`
- `engine/runtime/strategy_loader.py`
- legacy session/daemon pieces

Missing:
- production runner around `PaperRunner`
- live runner
- startup recovery
- config-driven strategy loading
- heartbeat freshness contract

## 12. Observability & Control Tower

Purpose: make every decision and failure explainable.

Responsibilities:
- decision journal
- runtime status
- dashboard
- alerts
- PnL attribution
- rejection reason summaries
- daily/weekly reports
- handoff logs

Current status: partial implementation.

Implemented:
- `engine/observability/journal.py`
- `engine/dashboard/`
- `.claude/logs/CURRENT.md`
- `.claude/logs/work_journal.jsonl`

Missing:
- attribution engine
- alerting
- dashboard integration with new runtime
- automated daily report

## Current Completion Estimate

Approximate architecture completeness: 35%-45%.

Solid foundation exists for contracts, registry, research datasets, and the
canonical signal pipeline. The largest missing first-class pieces are:

1. Position Management
2. complete Data Foundation
3. production Backtest & Simulation
4. Risk Management beyond account-level checks
5. Execution order state management
6. Live reconciliation
7. Observability/attribution

## Immediate Build Order

1. Harden `PositionManager` with configurable conflict modes.
2. Harden `PortfolioAccounting`.
3. Make `ProBacktestEngine` obey delayed fills, exits, funding, and attribution.
4. Connect `DataCatalog` to dataset builders/downloaders.
5. Add strategy/runtime loader enforcement from Strategy Office.
6. Build paper runner heartbeat and dashboard status.
7. Only then start full data download and ML strategy experiments.
