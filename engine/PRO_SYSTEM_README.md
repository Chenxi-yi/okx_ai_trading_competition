# Professional Personal Quant Engine

This is the new long-term architecture layer for the OKX personal quant system.
It is designed to absorb the existing competition engine while forcing all new
strategies into a cleaner research-to-live path.

## Implemented Foundation

```text
contracts/          MarketState, Signal, Decision, OrderIntent, Fill, PortfolioState
data/catalog.py     DataCatalog: dataset IDs, source, timeframe, coverage, status
data/instruments.py Instrument metadata snapshots
data/universe.py    Universe snapshots
download/           Data download dashboard/API: start, pause, resume, progress
features/           Feature and label builders
registry/           Strategy Office: IDs, params, status, performance, promotion
research/           Feature datasets, purged walk-forward, c-auto ML experiments
backtest/pro_engine.py
                    New Signal-pipeline historical backtest loop
arbitration/        PortfolioArbiter: EV/Kelly signal selection
position/           PositionManager: open/hold/close/reverse lifecycle gate
risk/               Account, strategy, instrument, and kill-switch gates
execution/router.py BacktestExecutionRouter, PaperExecutionRouter, LiveExecutionRouter
kit/                OKX Agent Trade Kit adapter: probes, gateway, supervisor
accounting/         Fill-driven perpetual portfolio accounting
runtime/            TradingPipeline, PaperRunner, PaperScheduler
runtime/strategy_loader.py
                    Strategy Office enforced runtime loading
runtime/market_provider.py
                    MarketState provider interface and OHLCV adapter
observability/      DecisionJournal append-only JSONL writer
execution/reconciliation.py
                    Local-vs-external position reconciliation
```

## Canonical Flow

```text
StrategyRegistry          -> allowed strategies and parameter sets
Strategy.generate(context) -> Signal[]
PortfolioArbiter          -> Decision[]
PositionManager           -> PositionIntent[] / adjusted Decisions
AccountRiskArbiter        -> approved/rejected RiskDecision[]
ExecutionRouter           -> OrderIntent -> Fill
PortfolioAccounting       -> PortfolioState from fills and mark prices
DecisionJournal           -> decisions.jsonl
```

Strategies are not allowed to import broker, subprocess, or OKX CLI. Live orders
belong only in `execution/router.py`, `execution/broker.py`, and the
`engine/kit/` Agent Trade Kit adapter.

## OKX Agent Trade Kit Boundary

Kit is embedded as the local OKX I/O layer:

```text
Strategy / Portfolio / Position / Risk
-> ExecutionRouter
-> KitExecutionGateway
-> OKX Agent Trade Kit CLI
-> OKX
```

Use Kit for token-free local work:

- low-frequency ticker/funding/open-interest probes
- account balance/position/bill synchronization
- order place/close/cancel/amend
- exchange-side protective TP/SL and algo orders
- local audit of OKX tool calls

Do not use Kit as a strategy brain. Strategy decisions remain in our Python
system so they can be researched, backtested, audited, and risk-gated.

Read-only supervisor:

```bash
python3 scripts/kit_supervisor.py --symbols BTC/USDT,ETH/USDT --no-account
```

Implementation notes:

```text
engine/kit/
scripts/kit_supervisor.py
.claude/knowledge/kit_integration.md
```

## Migration Pattern

Legacy target-weight strategies can be wrapped with:

```python
from strategies.protocol_adapter import TargetWeightStrategyAdapter
```

Custom strategies should implement the `contracts.Strategy` Protocol directly:

```python
class MyStrategy:
    strategy_id = "my_strategy"
    spec = StrategySpec(...)

    def generate(self, context: StrategyContext) -> list[Signal]:
        ...
```

## Small Account Rule

Order sizing is contract-aware. If a desired USDT size is too small to produce a
valid OKX contract quantity, the pipeline journals the decision but does not
execute a zero-size order.

## Next Migration Steps

1. Harden `PositionManager` with stop/target/time-stop exits and conflict policy.
2. Replace old target-weight backtests with `ProBacktestEngine` for all new strategies.
3. Build the complete data warehouse and register datasets in `DataCatalog`.
4. Convert `elite_flow` into a Signal-only tactical strategy.
5. Keep `yolo_momentum` in the speculative book with zero default live capital.
6. Connect `PaperRunner` to real market snapshots, heartbeat, and dashboard.

## Full Module Checklist

The complete architecture map is maintained in:

```text
.claude/knowledge/pro_quant_module_map.md
```

Top-level modules:

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

## Strategy Office CLI

```bash
python3 engine/registry_cli.py list
python3 engine/registry_cli.py show core_trend_momentum_v1
python3 engine/registry_cli.py register-strategy <strategy_id> \
  --name "Strategy Name" \
  --book core \
  --module strategies.my_strategy \
  --class-name MyStrategy \
  --status research \
  --risk-budget-json '{"max_nav_pct":0.30,"max_drawdown_pct":0.08,"max_leverage":1.5,"max_daily_loss_pct":0.02}'
python3 engine/registry_cli.py add-parameter-set <strategy_id> <strategy_id>.default \
  --params-json '{}' \
  --make-default
python3 engine/registry_cli.py promote core_trend_momentum_v1 backtest --reason "Spec complete"
python3 engine/registry_cli.py add-performance core_trend_momentum_v1 core_trend_momentum_v1.default \
  --mode backtest --start 2024-01-01 --end 2026-03-31 \
  --metrics-json '{"total_return_pct": 12.3, "sharpe_ratio": 0.9}'
python3 engine/registry_cli.py register-backtest-result core_trend_momentum_v1 core_trend_momentum_v1.default \
  engine/results/pro_backtest/<result_id> \
  --dataset-id <dataset_id>
```

All newly migrated strategies must enter through `register-strategy` with
`live_enabled=false` and `live_allocation_pct=0.0` unless a live promotion has
already been approved. `c-auto` is the main strategy research track; legacy
competition strategies remain isolated until explicitly prioritized.

## Research Lab

Materialize point-in-time features and labels:

```bash
python3 engine/research/feature_pipeline.py \
  --symbols BTC/USDT,ETH/USDT,SOL/USDT \
  --start 2024-01-01 --end 2026-03-31 \
  --timeframe 1h \
  --label-col fwd_ret_net_long_24 \
  --dataset-id c_auto_h24_research_v1 \
  --register-catalog
```

Run the first c-auto ML experiment from the registered parameter set:

```bash
python3 engine/research/c_auto_experiment.py \
  --dataset-id c_auto_h24_research_v1 \
  --parameter-set-id core_c_auto_h24_regression_v1.default \
  --register-performance
```

Research outputs live under `engine/data/research/c_auto/`. Registered results
are attached to the Strategy Office before any promotion from research to
backtest, paper, or live.

## Data Catalog

Datasets should be registered before they become research or backtest evidence:

```python
from data.catalog import DataCatalog

catalog = DataCatalog()
catalog.register_feature_dataset(
    "c_auto_h24_research_v1",
    "engine/data/features/c_auto_h24_research_v1",
)
```

The goal is to make every strategy result traceable to a dataset ID, timeframe,
symbol universe, date range, validation status, and artifact fingerprint.

## Data Download Control

Historical downloads are controlled by the standalone download module:

```bash
python3 scripts/data_download_server.py --port 8790
```

Open `http://127.0.0.1:8790` to start, pause, resume, and monitor training
history downloads. The control plane uses existing durable downloader outputs:

```text
engine/data/training_history/<run_id>/manifest.json
engine/data/training_history/<run_id>/status.json
engine/data/training_history/<run_id>/progress.jsonl
```

API surface:

```text
GET  /api/download/status?run_id=<optional>
GET  /api/download/runs
GET  /api/download/quality?run_id=<run_id>
POST /api/download/start
POST /api/download/pause
POST /api/download/resume
POST /api/download/register
```

Pause terminates the matching downloader process. Resume restarts the same
`run_id`; `fetch_training_history.py` skips already completed symbol/timeframe
jobs from `progress.jsonl`.

When a completed training-history run is observed, the module writes:

```text
engine/data/training_history/<run_id>/quality_summary.json
engine/data/catalog.json
```

The catalog dataset id is `raw_ohlcv_<run_id>_<timeframes>`. For example:

```text
raw_ohlcv_train_hist_134_5m_20240101_20260424_5m
```

## New Backtest Path

New strategies that implement `contracts.Strategy` should use
`backtest.pro_engine.ProBacktestEngine`. It runs the same canonical flow used by
paper/live:

```text
MarketState -> Strategy.generate -> PortfolioArbiter -> PositionManager
-> StrategyRisk -> AccountRisk -> InstrumentRisk -> BacktestExecutionRouter
-> PortfolioAccounting -> DecisionJournal
```

`ProBacktestEngine` defaults to delayed execution: signals generated from one
bar fill on the configured future bar, not the same bar close.

## Market Providers

Paper/live runners consume providers with this callable shape:

```python
provider() -> tuple[MarketState, dict[str, float]]
```

`OHLCVMarketProvider` is the first adapter for cached or in-memory OHLCV data.
Live websocket providers should implement the same callable shape.

The old `BacktestEngine` remains only for legacy target-weight strategies during
migration.

Backtest artifact directories can be attached to Strategy Office with:

```bash
python3 engine/registry_cli.py register-backtest-result \
  <strategy_id> <parameter_set_id> <artifact_dir> \
  --dataset-id <dataset_id>
```

## Paper Runtime

`PaperRunner` is intentionally single-cycle. Production-style looping belongs to
`PaperScheduler`:

```python
from runtime import PaperRunner, PaperScheduler, PaperSchedulerConfig

runner = PaperRunner(strategies, market_provider)
scheduler = PaperScheduler(
    runner,
    PaperSchedulerConfig(interval_sec=60, max_consecutive_errors=5),
)
scheduler.run()
```

The scheduler writes heartbeat/status JSON and halts after too many consecutive
errors. A stop file can be used by launchd/dashboard wrappers to request a
graceful stop.
