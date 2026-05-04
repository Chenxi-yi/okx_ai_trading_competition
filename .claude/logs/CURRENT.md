# Current Work Log

Last updated: 2026-05-04T23:40:00+0800

## User Goal

Build a long-term professional personal quant trading system on this computer.
Capital scale target: a few hundred USDT to 100k USDT. The system should allow
new strategies, parameter versions, research artifacts, backtests, paper trading,
and live allocation to evolve without damaging the architecture.

## Current System State

- Old engine process is stopped. Previous status file was stale.
- System-level refactor phase 1 is in place:
  - immutable contracts
  - strategy registry
  - portfolio arbiter
  - account risk arbiter
  - execution router
  - decision journal
  - canonical Signal -> Decision -> Risk -> Order -> Fill pipeline
- Strategy dimension has started with `core_c_auto_h24_regression_v1`.
- `c-auto` is the main strategy research track. Do not prioritize `elite_flow`
  migration unless the user explicitly asks.

## Completed In This Session

- Added `c-auto` as a core research strategy.
- Implemented `CAutoH24RegressionStrategy` as a signal-only strategy.
- Added strategy spec and registry entry.
- Fixed `engine/strategies/__init__.py` so the new adapter export does not hide
  existing strategy exports.
- Added durable handoff logs:
  - `.claude/logs/CURRENT.md`
  - `.claude/logs/work_journal.jsonl`
- Added `engine/research/c_auto_experiment.py` to evaluate registered `c-auto`
  parameter sets on materialized feature datasets.
- Updated `engine/PRO_SYSTEM_README.md` with Research Lab commands.
- Verified:
  - compileall passed
  - registry lists `core_c_auto_h24_regression_v1`
  - synthetic strategy smoke produced signals
  - synthetic paper pipeline smoke produced paper orders/fills
- `c_auto_experiment.py` smoke ran on `smoke_1h_mar2026_pipeline_v1`
- Added Strategy Office registration commands:
  - `python3 engine/registry_cli.py register-strategy ...`
  - `python3 engine/registry_cli.py add-parameter-set ...`
- Fixed registry public exports so the new registration CLI can import
  `RiskBudget`.

## Active Plan

User decided to pause strategy research until the full system architecture is
more complete. Current priority is architecture before large data download and
before real ML experiments.

User correctly identified that Position Management was missing as a first-class
architecture module. Architecture has now been expanded from coarse modules to a
12-module map.

1. Harden `PositionManager` conflict policy and partial exits.
2. Finish Data Foundation.
3. Finish new architecture backtest loop.
4. Harden portfolio accounting.
5. Finish paper runner and monitoring hooks.
6. Then download complete data.
7. Then resume `c-auto` and other strategy experiments.

## Next Expected Work

- Harden `ProBacktestEngine` with funding, exits, time stops, and attribution.
- Build `DataCatalog` usage into feature pipeline and data download jobs.
- Connect `PaperRunner` to real market snapshots, heartbeat, and dashboard.
- Only after this, download the full dataset and resume ML experiments.

## Architecture Additions After User Pause

- Added `engine/data/catalog.py` for dataset registration and traceability.
- Added `engine/accounting/portfolio_accounting.py` for fill-driven perpetual
  accounting.
- Added `engine/backtest/pro_engine.py` for the new Signal-pipeline historical
  backtest loop.
- Added `engine/runtime/paper_runner.py` as a single-cycle paper runner.
- Updated `engine/PRO_SYSTEM_README.md`.
- Compile passed.
- Minimal synthetic backtest smoke passed:
  - bars: 40
  - fills: 7
  - start NAV after first fill/fee: 9998.4998
  - end NAV: 10919.1078
  - fees: 7.2107

Important fix found during smoke: portfolio accounting initially treated
perpetual contracts like spot and deducted full notional on entry. This was
wrong. It is now corrected so cash changes through fees and realized PnL, while
unrealized PnL is mark-to-market.

## Architecture Gap Review

New module map added:

- `.claude/knowledge/pro_quant_module_map.md`

Top-level modules are now:

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

`Position Management` now has an early implementation:

- `engine/position/position_manager.py`
- `TradingPipeline` now routes `Decision[]` through `PositionManager` before
  account risk.
- Repeated same-side signals are held by default and do not create repeated
  orders.
- Opposite signals close existing exposure first; immediate reverse is disabled
  by default.
- Reduce-only closes use exact current contract count to avoid dust from
  notional-to-contract rounding.
- Existing positions can now generate reduce-only exits when target, stop, or
  time-stop triggers.

Latest smoke:

- repeated long over 18 bars: 1 fill only
- long then short: buy 2500, sell 2500 close, then sell 2455 after flat
- target exit: buy 2500, sell 2500 at target, final unrealized PnL 0

Accounting/Data additions:

- `PortfolioAccounting` now applies funding cashflows.
- `ProBacktestEngine` includes `total_funding_usdt` in summary.
- `feature_pipeline.py` supports `--register-catalog`.
- DataCatalog smoke registered `smoke_1h_mar2026_pipeline_v1` with 2232 rows.

Latest continuation:

- `PositionManager` now has same-symbol conflict policy.
- Default conflict mode: `winner_takes_symbol`.
- If two strategies produce decisions for the same instrument in one cycle, only
  the highest EV/Kelly/confidence decision proceeds.
- `ProBacktestResult` now includes `attribution`.
- `PaperRunner` writes a status JSON file.

Latest smoke:

- same-symbol long/short conflict: only `strat_a` executed.
- attribution output grouped by strategy ID.
- paper runner wrote `/tmp/paper_runner_status_smoke.json`.

Latest continuation 2:

- `PortfolioAccounting` now maintains strategy-level ledgers:
  - realized PnL
  - fees
  - funding
  - unrealized PnL by current position owner
- `ProBacktestResult.attribution` now reports:
  - fills
  - fees_usdt
  - funding_usdt
  - gross_turnover_contracts
  - realized_pnl_usdt
  - unrealized_pnl_usdt
  - net_pnl_usdt
- `PaperRunner` status now includes heartbeat fields:
  - runner_status
  - heartbeat_at
  - market_age_sec
  - stale

Latest smoke:

- attribution smoke:
  - fills: 2
  - realized_pnl_usdt: 28.994
  - fees_usdt: 2.0119976
  - funding_usdt: 0.756
  - net_pnl_usdt: 26.226002
- paper heartbeat smoke:
  - runner_status: ok
  - status file written
  - stale true for historical market timestamp, as expected

Latest continuation 3:

- `ProBacktestEngine` now writes formal artifacts:
  - `nav.csv`
  - `fills.csv`
  - `attribution.csv`
  - `summary.json`
  - `manifest.json`
- `ProBacktestConfig` now supports:
  - `result_dir`
  - `result_id`
- `PositionManagerConfig` now supports:
  - `partial_target_exit_pct`
  - `min_partial_contracts`
- Partial target exit behavior:
  - target hit can close only part of the position
  - stop/time-stop still close full remaining exposure
  - after a partial target exit, target is cleared so it does not repeatedly
    halve the position every bar

Latest smoke:

- partial target exit:
  - buy 2500
  - target hit sell 1250
  - remaining open 1250
  - remaining target cleared
  - artifacts written under `/tmp/pro_backtest_results/partial_exit_smoke_codex`

Latest continuation 4:

- Added `engine/runtime/paper_scheduler.py`.
- `PaperScheduler` wraps `PaperRunner` for production-style loops:
  - interval loop
  - `max_cycles` for tests
  - stop file support
  - heartbeat/status file
  - consecutive error counter
  - halt after `max_consecutive_errors`
- Updated runtime exports and `engine/PRO_SYSTEM_README.md`.

Latest smoke:

- normal scheduler:
  - `max_cycles=2`
  - completed with cycles=2
  - consecutive_errors=0
- error scheduler:
  - provider raises `RuntimeError`
  - halted after 2 consecutive errors
  - status file written

Latest continuation 5:

- Added `registry_cli.py register-backtest-result`.
- It reads a `ProBacktestEngine` artifact directory:
  - `summary.json`
  - `manifest.json`
- It creates a Strategy Office `PerformanceRecord` with:
  - mode `backtest`
  - metrics from summary
  - fees/funding costs from summary
  - artifact directory as `decision_journal_path`
  - optional `dataset_id`
- Smoke:
  - compile passed
  - CLI help works
  - did not write smoke artifacts into real registry

Latest continuation 6:

- Added Data Foundation:
  - `engine/data/instruments.py`
  - `engine/data/universe.py`
- Added Risk layers:
  - `engine/risk/instrument.py`
  - `engine/risk/strategy.py`
  - `engine/risk/kill_switch.py`
- `TradingPipeline` now includes:
  - kill switch
  - strategy risk
  - instrument risk
- Added runtime registry enforcement:
  - `engine/runtime/strategy_loader.py`
- Added reconciliation skeleton:
  - `engine/execution/reconciliation.py`
- `ProBacktestEngine` now supports delayed execution:
  - decision bar and execution bar are separated
- Added report builder:
  - `engine/observability/reporting.py`

Latest smoke:

- instrument snapshot read/write passed
- reconciler ok/mismatch passed
- delayed execution filled next bar
- backtest report generated
- StrategyLoader loaded `core_c_auto_h24_regression_v1`

Latest continuation 7:

- Added `engine/runtime/market_provider.py`.
- `OHLCVMarketProvider` implements callable provider shape:
  - returns `MarketState`
  - returns `mark_prices`
  - advances through OHLCV timestamps sequentially
- PaperRunner smoke with `OHLCVMarketProvider` advanced from 00:00 to 01:00.

Latest continuation 8:

- Extracted the old launcher download widget/control plane into a standalone
  Data Download module.
- Added `engine/download/manager.py`:
  - discovers existing downloader processes
  - summarizes `manifest.json`, `status.json`, and `progress.jsonl`
  - starts new `training_history` runs
  - pauses by terminating the matching downloader process
  - resumes by restarting the same `run_id`
- Added `engine/download/server.py` and `scripts/data_download_server.py`.
- Added dedicated frontend under `engine/download/static/`.
- Verified the standalone server at `http://127.0.0.1:8790`:
  - `/api/health`
  - `/api/download/status`
  - `/`

Next data work:

- Add derivatives structure downloader control to the same dashboard.

Latest continuation 9:

- Completed automatic DataCatalog registration for completed training-history
  download runs.
- Added `engine/download/quality.py`.
- Download status now writes:
  - `quality_summary.json`
  - `engine/data/catalog.json`
- Registered current completed 5m run:
  - dataset_id: `raw_ohlcv_train_hist_134_5m_20240101_20260424_5m`
  - rows: 20,555,723
  - symbols: 134
  - validation_status: `ok`
- Added API:
  - `GET /api/download/quality?run_id=<run_id>`
  - `POST /api/download/register`
- Frontend now displays Catalog state, quality state, row count, and median
  coverage.
- Hardened `DataCatalog`:
  - package import now works from repo root
  - writes are atomic to avoid dashboard polling reading partial JSON

Next data work:

- Add derivatives structure downloader control to the same dashboard.
- Add row-level gap/staleness audits beyond downloader progress summaries.

Latest continuation 10:

- Researched OKX Agent Trade Kit local role.
- Decided architecture boundary:
  - our system remains strategy/risk/position/accounting brain
  - Kit becomes local token-free OKX I/O worker
- Added `engine/kit/`:
  - `schemas.py`
  - `client.py`
  - `market_probe.py`
  - `account_probe.py`
  - `execution_gateway.py`
  - `supervisor.py`
- Added runnable supervisor:
  - `scripts/kit_supervisor.py`
- `LiveExecutionRouter` now executes through `KitExecutionGateway`.
- Added durable architecture note:
  - `.claude/knowledge/kit_integration.md`
- Safety:
  - default profile is `demo`
  - live trade commands require both `LIVE_TRADING=true` and `allow_live=True`
- Verified:
  - compile passed
  - CLI argv construction passed
  - live trade gate blocks by default
  - real read-only supervisor call fetched BTC ticker/funding/open interest

Next Kit work:

- Add reconciliation ingestion from `AccountProbe.positions/bills`.
- Add exchange-side protective order policy after Position/Risk approval.

Latest continuation 11:

- Upgraded OKX Agent Trade Kit:
  - `@okx_ai/okx-trade-cli`: 1.2.7 -> 1.3.2
  - `@okx_ai/okx-trade-mcp`: 1.2.7 -> 1.3.2
- Verified:
  - `okx --version`: `1.3.2 (1bb94dcd)`
  - global npm package versions are both 1.3.2
  - default profile `okx diagnose --cli` passed
  - read-only market ticker works
  - read-only Kit supervisor works after upgrade
- `okx diagnose --all` returns non-zero only because no MCP client config is
  registered yet; MCP package/handshake itself passed.
- Demo profile private auth failed:
  - `okx --profile demo diagnose --cli`
  - error: `Invalid OK-ACCESS-KEY`
  - implication: refresh demo API key before demo private-account calls.

Research idea recorded:

- Added `.claude/knowledge/strategy_ideas/btc_alt_beta_amplifier.md`.
- Candidate ID: `tactical_btc_alt_beta_amplifier_v0`.
- User hypothesis: BTC small up/down move can trigger amplified altcoin
  long/short moves.
- Status: idea / waiting research, not a live strategy.

Next Kit work:

- Refresh demo API key if demo private testing is needed.
- Optionally register MCP with `okx setup --client claude-code`.
- Add reconciliation ingestion from `AccountProbe.positions/bills`.
- Add exchange-side protective order policy after Position/Risk approval.

## Latest Smoke Result

Command:

```bash
python3 engine/research/c_auto_experiment.py \
  --dataset-id smoke_1h_mar2026_pipeline_v1 \
  --max-folds 2 \
  --out-id smoke_c_auto_experiment_codex \
  --notes smoke-test
```

Output artifacts:

- `engine/data/research/c_auto/smoke_c_auto_experiment_codex/manifest.json`
- `engine/data/research/c_auto/smoke_c_auto_experiment_codex/metrics.json`
- `engine/data/research/c_auto/smoke_c_auto_experiment_codex/predictions.parquet`

Smoke metrics are not strategy evidence. They were negative on the tiny smoke
dataset and should not be registered as performance.

## Safety Notes

- Do not place live orders without explicit user authorization.
- All live orders must go through Agent Trade Kit / OKX CLI path.
- `c-auto` must remain signal-only; execution and sizing belong to system layers.
