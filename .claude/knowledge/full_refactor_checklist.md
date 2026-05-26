# Full Refactor Checklist

Last updated: 2026-05-19T00:18:00+08:00

This checklist tracks the full modular refactor. A new conversation must use
this file plus `runtime_strategy_governance.md` before modifying trading code.

Legend:

- `[x]` done in code or docs
- `[~]` partial, compatibility bridge still exists
- `[ ]` not done

## Architecture Contract

- [x] Write runtime governance constraints.
- [x] Write full refactor blueprint.
- [x] Link architecture constraints from `AGENTS.md`.
- [x] Log architecture drift in the error registry.

## Strategy Office

- [x] StrategyRecord includes runtime specification.
- [x] StrategyRecord includes data dependency declarations.
- [x] Registry exposes `runnable_strategies(environment)`.
- [~] Existing strategy JSON has runtime fields for current live strategies.
- [ ] Every runnable strategy declares data dependencies and max freshness.
- [x] Promotion CLI enforces runtime, data fields, and live evidence-quality
  metric gates.
- [x] Strategy Office rejects live enablement without runtime and data dependencies.
- [x] Architecture boundary checker runs in the normal verification flow.

## Environment Runner

- [x] Add canonical `engine/runtime/environment_runner.py`.
- [x] Runner reads Strategy Office instead of hard-coded strategy ids.
- [x] Runner maps environment to OKX profile.
- [x] Runner generates strategy adapter launch plans.
- [~] Launcher strategy selection uses `StrategyRegistry.runnable_strategies`.
- [x] Launcher start endpoint delegates strategy selection and launch planning to
  `EnvironmentRunner`.
- [x] Runner writes structured status for process/data/committee/position/execution/accounting;
  ownership reconciliation status is surfaced and refreshed by the launcher-owned
  background reconcile scheduler.
- [~] Direct strategy-specific start endpoints are compatibility-only.

## Data Readiness

- [x] Add `DataReadinessProbe`.
- [x] Readiness supports file freshness checks.
- [~] Registry data dependencies filled for current live trend strategies.
- [x] Readiness supports dataset manifests, feature manifests, and row coverage.
- [x] Data artifact read compatibility is centralized in `engine/data/frame_store.py`;
  migrated parquet repair is a data maintenance concern, not strategy logic.
- [x] Readiness status is shown as a separate health dimension in dashboard,
  including required/ready counts and blocking dependency reasons.

## Strategy Adapter Layer

- [~] Define normalized `CandidateTrade` / `Signal` adapter output for legacy scripts;
  contract exists and live scripts now persist candidate contracts before committee.
- [~] `run_research_sleeve_paper.py` routes live entry/close through
  `ExecutionRouter` / `KitExecutionGateway`; full signal-only migration pending.
- [~] `run_c_auto_v2_micro_live.py` routes live entry/close through
  `KitExecutionGateway`; full signal-only migration pending.
- [~] Trend, US-equity, c-auto, BTC sleeves emit normalized candidates where they
  pass through live research/c-auto adapters; inactive legacy competition
  strategies are now marked retired and blocked from launcher start paths.

## Investment Committee

- [~] Existing committee module can approve/reject some candidates.
- [ ] Committee is the only path from candidate to approved trade plan.
- [ ] Committee records all rejected and accepted decisions with feature refs.
- [~] Committee applies fee/slippage profitability and same-symbol conflict gates;
  round-trip fee drag is now part of arbitration, full slippage model still pending.
- [ ] Committee assigns final margin, notional, leverage, stop, target.

## Position Manager

- [x] Existing `engine/position/position_manager.py` manages open/add/close/hold.
- [x] PositionIntent contract exported from `engine/contracts`.
- [~] Live strategy scripts no longer own generic stop/target/time-stop lifecycle;
  c-auto and research live adapters translate local caches through
  `LivePositionLifecycleService` into `PositionManager` reduce-only intents.
- [~] Position manager owns strategy lifecycle exit intents; environment flatten
  still uses account-control endpoints because it is an operator command, not a
  strategy decision.
- [ ] Position manager blocks new entries when reconciliation is unknown.
- [ ] Stop-loss loss is capped against account NAV before every entry.

## Risk Manager

- [~] Account/strategy/instrument risk modules exist.
- [ ] Risk manager is called for every approved committee decision.
- [ ] Daily loss, drawdown, exposure, and stop-loss-at-risk are hard gates.
- [x] Kill switch blocks new entries while allowing reduce-only exits.

## Execution

- [~] `ExecutionRouter` and Kit gateway exist.
- [~] Current live adapters route entry/close through execution/kit; older
  inactive competition strategies are excluded by the boundary checker until
  migrated or retired.
- [~] Legacy direct OKX calls in strategy scripts are guarded behind environment
  runner context; legacy competition runner files are explicitly retired and the
  launcher refuses to start them.
- [ ] Protective stop failure immediately reconciles and flattens.
- [x] All current live adapter orders record strategy, decision id, fees, fills,
  and profile through `LiveOwnershipJournal`.

## Accounting And Reconciliation

- [~] Fill-driven accounting exists for paper/backtest.
- [~] Live state can be rebuilt from the ownership journal and compared against
  exchange positions via `scripts/reconcile_live_ownership.py`; the script now
  also writes accounting performance summaries from exchange fills/bills, with
  unrelated bills excluded from strategy PnL. Full historical attribution depends
  on new orders being journaled after this migration.
- [~] Internal JSON files remain as strategy caches, but live ownership now has an
  accounting-owned append-only journal.
- [~] Unknown/orphaned exchange positions block new entries in live strategy
  adapters and launcher start preflight; ownership reconcile is now refreshed in
  the background and on launcher starts.
- [x] Flatten confirms OKX positions, open orders, and algo orders are actually zero.

## Control Tower / Frontend

- [~] Frontend Start/Pause/Flatten calls environment-like endpoints.
- [x] Buttons report process truth and account reconciliation truth; runtime cards
  include ownership reconciliation, strategy cards include accounting fill/bill
  counts, and data readiness has a separate detail panel.
- [x] Stale state files are never displayed as live running without process proof.
- [x] Dashboard shows alive / ready / blocked / stale / ownership-reconciled
  separately, with compact exchange reconciliation detail.

## Completion Bar

The refactor is not complete until:

1. Starting personal or competition never names a strategy outside Strategy Office.
2. A live entry cannot happen without committee, position, risk, execution, and
   accounting records.
3. No strategy script owns exchange orders.
4. Pause and flatten return verified account/process state.
5. A new strategy can be added through registry + adapter + data dependencies
   without editing launcher control flow.
