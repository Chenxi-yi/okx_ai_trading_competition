# Professional Personal Quant System Architecture

This document is the system-level contract for the OKX personal quant engine.
Every new strategy, research idea, data source, backtest, and live execution path
must fit this architecture unless there is an explicit written exception.

The target capital range is a few hundred USDT to 100,000 USDT. The system is
therefore optimized for durability, iteration speed, auditability, and realistic
execution rather than institutional HFT complexity.

## System-Level Architecture

The engine is organized as twelve large modules. File and class design must map
back to one of these modules before implementation. The detailed checklist lives
in `.claude/knowledge/pro_quant_module_map.md`.

```text
┌─────────────────────────────────────────────────────────────┐
│              Professional Personal Quant System              │
└─────────────────────────────────────────────────────────────┘

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

### Module Responsibilities

| Module | Responsibility | Primary Directories |
|---|---|---|
| 0. Contracts & Schemas | Shared dataclasses and Protocols | `engine/contracts/` |
| 1. Data Foundation | Raw/live data, coverage, quality, metadata, catalog | `engine/data/` |
| 2. Feature & Label Store | Point-in-time features, labels, registries, validation | `engine/features/` |
| 3. Strategy Office | Strategy IDs, specs, params, status, performance, promotion | `engine/registry/`, `engine/strategies/specs/` |
| 4. Research Lab | Experiments, walk-forward, reports, Monte Carlo | `engine/research/`, `engine/results/` |
| 5. Backtest & Simulation | Historical same-path replay and simulated fills | `engine/backtest/` |
| 6. Portfolio Construction | Signal arbitration, allocation, conflict resolution | `engine/arbitration/`, `engine/portfolio/` |
| 7. Position Management | Open/add/reduce/close/reverse/hold lifecycle | `engine/position/` |
| 8. Risk Management | Instrument, strategy, account, kill-switch risk | `engine/risk/` |
| 9. Execution Management | Backtest/paper/live order routing and state | `engine/execution/` |
| 10. Portfolio Accounting & Reconciliation | Fill-driven positions, PnL, NAV, OKX reconciliation | `engine/accounting/` |
| 11. Runtime Orchestration | Scheduler, runners, heartbeat, recovery | `engine/runtime/` |
| 12. Observability & Control Tower | Journals, attribution, dashboard, alerts, handoff logs | `engine/observability/`, `engine/dashboard/`, `.claude/logs/` |

## Core Principles

1. Strategies emit signals; they do not place orders.
2. Backtest, paper, and live share the same strategy, arbitration, risk, and
   accounting path. Only the data adapter and execution router may change.
3. All market data, features, labels, signals, decisions, orders, fills, and
   outcomes must be point-in-time auditable.
4. Account-level risk has final authority over every order.
5. Every strategy starts in research, graduates through paper trading, and only
   then receives live capital.
6. High-risk or contest-style strategies are isolated from the core capital book.
7. Stale data is a hard risk event, not a warning to ignore.

## 8-Layer Structure

The 8-layer structure is the production workflow view of this architecture. It
is the canonical path from raw data to personal-account capital.

```text
1. Automated Data Update
   Refresh OHLCV, derivatives, instrument metadata, quality reports, and feature
   inputs. Stale or incomplete data blocks downstream promotion.

2. Automated Strategy Research
   Generate and test hypotheses, parameter sets, feature variants, and failure
   explanations. Research may create candidates, but it cannot allocate capital.

3. Automated Strategy Evaluation
   Run backtests, walk-forward checks, stress tests, cost sensitivity, leakage
   checks, and minimum-sample gates. Passing evaluation promotes a strategy to
   paper eligibility.

4. Paper Trading
   Run the strategy with production-like accounting, stops, leverage policy,
   and decision journals, but without exchange orders.

5. Competition Account Production
   The first real-money environment. Use the shared competition account as a
   small-capital production canary after paper passes. Demo is not on the
   critical path.

6. Personal Account Production
   The highest-value account. Strategies only reach this layer after successful
   competition-account evidence and explicit owner approval.

7. Investment Committee
   Multiple strategies submit trade plans. The committee approves/rejects each
   plan and assigns NAV budget, gross/net exposure, stop-loss budget, and
   leverage. The leverage weapon library lives in
   `engine/arbitration/leverage_policy.py` with policy documentation in
   `engine/config/committee_leverage_policy.json`. It includes single-position
   NAV loss caps, stop-to-margin caps, daily-loss veto, same-symbol duplicate
   veto, same-side concentration scalar, Kit disagreement scalar, and an
   explicit aggressive-leverage gate.

8. Position Management And Strategy Review
   Position management owns open-position lifecycle, stops, take-profit,
   time-stops, reductions, and reconciliation. Daily strategy review feeds
   failures and required changes back into automated research.
```

Environment promotion order is hard-coded as:

```text
paper -> competition -> personal
```

The current target capital basis is `3000 USDT`. Monthly return target is `20%`
as an objective for research and allocation design, not as a guaranteed outcome.
Risk controls must preserve survival first: a target return never overrides
freshness gates, stop requirements, allocation caps, or kill switches.

## Target Module Map

```text
engine/
  contracts/              system-wide immutable dataclasses and Protocols
  registry/               strategy IDs, parameters, performance, lifecycle
  data/                   OKX adapters, parquet store, universe, quality checks
  features/               point-in-time feature builders, labels, registry
  regime/                 market regime classifiers
  strategies/             Strategy Protocol implementations only
  arbitration/            signal conflict resolution and expected-value ranking
  position/               position lifecycle and position-change intents
  risk/                   account, strategy, instrument, and kill-switch risk
  execution/              backtest/paper/live execution routers and OKX broker
  portfolio/              allocation and portfolio construction helpers
  accounting/             fill-driven perpetual accounting and reconciliation
  research/               datasets, backtests, walk-forward, experiments
  runtime/                scheduler, session lifecycle, reconciliation
  observability/          decision journal, metrics, alerts, dashboard API
  config/                 system, risk, universe, strategy, profile config
```

Existing modules should be migrated into this map instead of rewritten blindly:

- `execution/broker.py` remains the OKX Agent Trade Kit CLI bridge.
- `data/fetcher.py` remains the first OKX historical/cache adapter.
- `features/` and `research/feature_pipeline.py` become the feature store seed.
- `core/` provides useful LEAN-style pipeline concepts.
- `backtest/runner.py` provides the correct same-path backtest pattern.
- `logging_/structured_logger.py` evolves into the decision journal.

## Strategy Office

`Strategy Office` is the source of truth for every strategy. A strategy must be
registered before it can be researched, backtested, paper traded, or live
enabled.

Required records:

```text
StrategyRecord      strategy_id, name, book, status, version, module path,
                    risk budget, default parameter set, timestamps
ParameterSet        parameter_set_id, strategy_id, version, params, notes
PerformanceRecord  strategy_id, parameter_set_id, mode, window, metrics,
                    costs, journal path, created_at
PromotionRecord     strategy_id, from_status, to_status, reason, evidence
```

Allowed strategy status flow:

```text
idea -> research -> backtest -> paper -> live
                         └-------> paused -> retired
```

Runtime may only load strategies whose registry status and environment permit
execution. Live allocation defaults to zero unless Strategy Office explicitly
enables it.

## Canonical Trading Flow

Every strategy must flow through this pipeline:

```text
DataAdapter
  -> MarketState
  -> FeatureBuilder
  -> RegimeClassifier
  -> Strategy.generate()
  -> Signal[]
  -> PortfolioArbiter
  -> Decision[]
  -> PositionManager
  -> PositionIntent[]
  -> AccountRisk
  -> OrderIntent[]
  -> ExecutionRouter
  -> Fill[]
  -> PortfolioAccounting
  -> DecisionJournal
```

No strategy may call `okx`, `subprocess`, `Broker`, or exchange clients directly.
Direct order placement belongs only in `execution/`.

## Contracts

The future `engine/contracts/` package is the highest-priority foundation.
It defines the objects shared by research, backtest, paper, and live.

Required contracts:

```text
MarketState       timestamp, universe, OHLCV/orderbook/funding/OI snapshots
RegimeLabel       regime name, confidence, features, classifier version
Signal            strategy_id, symbol, side, entry, target, stop, p_target,
                  adverse_pct_estimate, horizon, confidence, metadata
Decision          decision_id, accepted signal, rejected signals, size_usdt,
                  reason, arbiter_id, timestamp
OrderIntent       decision_id, inst_id, side, size_contracts, order_type,
                  limit_price, leverage, reduce_only, profile
Fill              decision_id, order_id, fill_price, fill_size, fee, timestamp
Position          symbol, side, entry_price, size_contracts, opened_at,
                  decision_id, target, stop, time_stop
PortfolioState    nav, free_usdt, positions, strategy_usage, risk_state
DecisionEvent     feature snapshot, signal, decision, order, fill, outcome
PositionIntent    open/add/reduce/close/reverse/hold, target contracts/notional,
                  reduce_only, exit reason, ownership metadata
```

Contracts should be frozen dataclasses where possible. Breaking contract changes
are major architecture changes and require migration notes.

## Position Management

Position management is a first-class module. It is not part of risk and not part
of execution.

Responsibilities:
- Convert accepted portfolio decisions into desired position changes.
- Prevent repeated signals from endlessly adding to the same position.
- Decide open, add, reduce, close, reverse, or hold.
- Own targets, stops, time stops, signal expiry, and partial exit policy.
- Resolve same-symbol conflicts across strategies.
- Preserve ownership metadata: strategy_id, parameter_set_id, decision_id.

Correct order:

```text
PortfolioConstruction -> PositionManagement -> RiskManagement -> Execution
```

Risk may reduce or reject a position change. Execution may only implement the
approved order. Neither module owns the position lifecycle.

## Strategy Placement Rules

Every new strategy must be classified before implementation.

### Core Book

Longer-lived strategies that can manage most capital.

Examples:
- Cross-sectional momentum or mean reversion.
- Funding/carry.
- Regime-aware trend following.
- ML prediction with stable walk-forward evidence.

Requirements:
- Uses standardized features and labels.
- Has out-of-sample backtests.
- Has realistic cost, funding, and slippage assumptions.
- Has a declared holding period and risk budget.
- Outputs `Signal[]`, never orders.

### Tactical Book

Shorter-lived opportunity strategies.

Examples:
- Crowding squeeze.
- Breakout continuation.
- Event or listing behavior.
- Orderbook imbalance with medium-frequency execution.

Requirements:
- Has stricter data freshness checks.
- Has liquidity and spread gates.
- Has per-strategy kill switch.
- Starts with small allocation and paper/live shadow logging.

### Speculative Book

High-risk, high-convexity, contest-style, or martingale systems.

Examples:
- YOLO momentum.
- Monster lottery.
- Extreme leverage experiments.

Requirements:
- Isolated from core capital.
- Cannot share order path directly with core strategies.
- Must have Monte Carlo or path-stress simulation.
- Must have explicit max loss budget.
- Default live allocation is zero until manually enabled.

## Strategy Spec Requirement

Every strategy needs a spec before implementation:

```text
strategy_id:
hypothesis:
book: core | tactical | speculative
timeframe:
holding_period:
symbols_or_universe:
required_data:
required_features:
allowed_regimes:
entry_logic:
exit_logic:
position_sizing:
risk_budget:
expected_failure_modes:
backtest_window:
paper_requirement:
live_enable_default:
owner_notes:
```

If a strategy cannot fill this spec, it stays in research notes and does not
enter runtime.

## Backtest Rules

These are hard rules learned from previous backtest failures.

1. Backtest and live must share strategy, arbitration, risk, and accounting code.
2. A strategy may only see data at or before the current timestamp.
3. Signals generated from a bar cannot fill on the same completed bar close.
4. Mark price and execution price must be separated.
5. Funding, volume, ADV, OI, and similar market state inputs must be shifted or
   otherwise proven point-in-time safe.
6. Universe membership must be point-in-time. A newly listed symbol cannot leak
   into old historical windows.
7. Tradeability requires enough history, enough liquidity, valid open/close
   data, acceptable spread, and valid contract metadata.
8. Fees, slippage, funding, and turnover must be included in every report.
9. Portfolio-level risk controls must run inside backtests.
10. Monte Carlo is required for high-leverage, martingale, or path-dependent
    strategies.

The current `backtest/runner.py` same-pipeline pattern is preferred over legacy
target-weight-only backtests.

## Backtest Execution Model

Baseline bar-level execution:

```text
signal timestamp:  bar close or scheduled decision time
execution price:   next available open, VWAP proxy, or configured replay price
mark price:        close or mark/index price used for NAV
fees:              taker/maker by order type
slippage:          spread floor + square-root market impact
funding:           shifted point-in-time funding rate
turnover:          absolute notional traded
```

For small accounts, fixed spread/fee assumptions can dominate results. Reports
must therefore show total fees, slippage, funding, and turnover separately.

## Data And Feature Rules

Data is a product, not a side effect.

Required checks:
- UTC internal timestamps.
- No duplicate index rows.
- Monotonic index.
- Explicit missing data handling.
- Freshness threshold by timeframe.
- Symbol normalization between OKX `BTC-USDT-SWAP` and ccxt `BTC/USDT`.
- Contract metadata snapshot: contract value, lot size, min size, max market
  size, max leverage, tick size.
- Universe filters for stablecoins, TradFi perps, inactive instruments, low
  liquidity, and excessive spread.

Feature rules:
- All feature rows are indexed by `(timestamp, symbol)`.
- Every feature has registry metadata: family, source columns, lookback,
  frequency, live availability, expected NaN warmup, version.
- Labels are built separately from strategy code.
- Feature validation failures block promotion to strategy testing.
- Research and live feature definitions must share code.

## Risk Architecture

Risk is layered. Later layers can reduce or reject orders from earlier layers.

```text
InstrumentRisk   contract metadata, min size, spread, depth, leverage cap
StrategyRisk     strategy budget, per-strategy drawdown, cooldown, regime gates
AccountRisk      total gross/net exposure, drawdown, correlation, daily loss
KillSwitch       stale data, API errors, reconciliation mismatch, manual halt
```

Account risk has final authority.

Minimum required controls:
- Max daily loss.
- Max peak-to-trough drawdown.
- Max gross leverage.
- Max net exposure.
- Max symbol exposure.
- Max strategy allocation.
- Correlation/crowding reduction.
- Data stale open-trade block.
- Reconciliation mismatch: reduce-only mode until fixed.

## Execution Rules

Live orders must go through OKX Agent Trade Kit CLI via the execution layer.

Hard constraints:
- No raw ccxt order placement.
- No strategy-level subprocess order calls.
- `sz` means contracts, not coins.
- Position mode is net.
- Dry-run or paper validation before enabling new live order flow.
- Every order must carry local `decision_id` in logs.
- Every fill must reconcile against account state.

Execution routers:

```text
BacktestExecutionRouter   simulated fills from historical replay
PaperExecutionRouter      live market data, simulated fills
LiveExecutionRouter       live market data, OKX CLI orders
```

Only the router changes between backtest, paper, and live.

## Observability And Decision Journal

Every trade must be explainable after the fact.

A decision journal entry must join:

```text
timestamp
environment
dataset / feature version
market state freshness
regime
feature snapshot reference
raw strategy signal
rejected competing signals
arbiter decision
risk adjustments
order intent
exchange response
fill
fees / slippage / funding
MFE / MAE / forward outcome
post-trade attribution
```

Runtime status files must include staleness metadata. A stale `summary.json`
must never be reported as a running engine.

## Promotion Workflow

Strategies move through stages:

```text
idea
  -> research spec
  -> feature/label dataset
  -> deterministic backtest
  -> walk-forward / OOS
  -> cost and stress tests
  -> paper trading
  -> small live allocation
  -> monitored allocation increase
```

Promotion gates:
- No known leakage.
- No unresolved data validation failure.
- OOS result is acceptable.
- Fees/slippage/funding do not erase the edge.
- Drawdown fits the assigned book.
- Paper logs match expected signal and execution behavior.
- Risk owner explicitly enables live allocation.

## Migration Plan From Current Engine

Phase 1: Add contracts and decision journal without changing behavior.

Phase 2: Wrap existing `Broker` behind execution routers.

Phase 3: Convert `elite_flow`, `yolo_momentum`, and baseline strategies into
Strategy Protocol adapters that emit `Signal[]`.

Phase 4: Move existing feature and label builders into a versioned feature store.

Phase 5: Replace direct strategy execution loops with runtime scheduler +
arbiter + account risk + execution router.

Phase 6: Make all backtests write the same decision journal shape as paper and
live.

## Non-Negotiable Review Checklist

Before any strategy enters paper or live:

- [ ] Strategy has a written spec.
- [ ] Strategy emits signals only.
- [ ] Required data exists and passes quality checks.
- [ ] Features are registered and point-in-time safe.
- [ ] Labels are separate from strategy code.
- [ ] Backtest uses delayed execution, not same-close fill.
- [ ] Universe membership is point-in-time.
- [ ] Costs include fees, slippage, funding, and turnover.
- [ ] Account risk runs in the backtest.
- [ ] Decision journal records signal, decision, order, fill, and outcome.
- [ ] Live route uses OKX Agent Trade Kit CLI only.
- [ ] Live allocation defaults to zero unless explicitly enabled.
