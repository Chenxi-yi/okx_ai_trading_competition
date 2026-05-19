# Runtime Strategy Governance

Last updated: 2026-05-18T14:45:00+08:00

This is the hard operating contract for strategy lifecycle, runtime startup,
investment committee approval, and position management. It extends
`.claude/knowledge/pro_quant_system_architecture.md` and overrides older notes
that describe individual strategies as standalone launchers.

The implementation roadmap lives in
`.claude/knowledge/full_refactor_blueprint.md`. Use that blueprint when changing
code structure, not only when adding documentation.

## Non-Negotiable Boundaries

1. A strategy is only a signal producer.
2. A runner is only an environment orchestrator.
3. The strategy management module is the source of truth for what may run.
4. The investment committee decides whether a signal becomes a trade plan.
5. Position management is the trader. It owns open-position lifecycle after the
   committee approves a plan.
6. Execution management is the only layer allowed to place, cancel, or close
   exchange orders.
7. Data readiness is a launch gate, not a warning.

No strategy, including `c_auto`, US-equity sleeves, trend pullback sleeves,
BTC swing sleeves, or Elite Flow, may be treated as a launcher. A strategy may
run only because an environment runner loaded its registry record and started
it for an allowed environment.

## Canonical Runtime Flow

```text
User clicks Start Personal / Start Competition
  -> environment runner starts
  -> runner reads Strategy Office registry
  -> runner filters by environment, status, runtime.enabled, live_enabled
  -> runner checks data/feature readiness for every selected strategy
  -> runner starts one supervised process per registered strategy
  -> strategy process emits Signal / CandidateTrade only
  -> Investment Committee accepts/rejects and sizes the trade plan
  -> Position Manager opens/adds/reduces/closes/holds positions
  -> Risk Manager applies account, strategy, symbol, and stop-loss limits
  -> Execution Router sends orders through OKX Agent Trade Kit
  -> Accounting reconciles fills, bills, positions, NAV, and ownership
  -> Observability reports health, decisions, positions, and errors
```

Stop and flatten use the same ownership boundary:

```text
User clicks Pause
  -> environment runner stops all registry-selected strategy processes
  -> position manager continues or enters stop-only mode as configured

User clicks Flatten
  -> position manager requests close intents for account positions
  -> risk/execution route close orders
  -> accounting reconciles fills and confirms account flat
```

The frontend must not start strategy scripts directly. It calls the environment
runner. The runner may start zero, one, or many strategy processes based only on
registry state.

## Strategy Office Contract

`engine/config/strategy_registry.json` is the current Strategy Office source of
truth until it is migrated into `engine/registry/` storage. Every runnable
strategy must have:

```text
strategy_id
status                  idea | research | backtest | paper | live | paused | retired
owner                   personal | competition | shared
module                  signal implementation location
default_parameter_set_id
risk_budget             max_nav_pct, max_daily_loss_pct, max_drawdown_pct, max_leverage
runtime.enabled
runtime.runner          runner type, not strategy identity
runtime.allowed_environments
runtime.interval_sec
runtime.priority
runtime.state_id
live_enabled
live_allocation_pct
updated_at
```

Allowed promotion path:

```text
idea -> research -> backtest -> paper -> competition-live -> personal-live
                         |          |             |
                         v          v             v
                      paused     paused        retired
```

Promotion into Strategy Office requires evidence:

- data dependencies are listed by dataset id and feature version;
- all required features are produced by the unified data update program;
- backtest or paper result is registered as a performance record;
- risk budget and parameter set are registered;
- runtime environment is explicit;
- owner approval is recorded for live enablement.

Research scripts may create candidates and evidence. They must not create
runtime behavior outside Strategy Office.

## Data And Feature Readiness

Before a strategy can move from research into paper or live, its data contract
must be satisfied by the unified data update program. A strategy may not depend
on an ad hoc feature file that only the research script knows how to produce.

Each strategy must declare:

```text
raw_data_sources        OHLCV, funding, OI, long/short, equity close, orderbook
feature_builders        code paths and output dataset ids
feature_timeframes      e.g. 5m, 15m, 1h, 4h, 1d
max_feature_age_sec
minimum_coverage
quality gates
runtime readiness probe
```

The data updater owns freshness. The runner must refuse to start a strategy if
its declared data or feature dependencies are missing, stale, or below quality
threshold. A strategy process must also fail closed if a required live feature
check fails during a cycle.

## Investment Committee Contract

Strategies submit candidate signals. They do not choose final account exposure.
The committee receives a normalized trade proposal:

```text
strategy_id
parameter_set_id
symbol / inst_id
side
entry reference
target
stop
time stop
expected edge
confidence
feature snapshot id
max adverse loss estimate
requested margin/notional/leverage
reason and invalidation thesis
```

The committee must:

- reject stale or incomplete signals;
- resolve same-symbol and cross-strategy conflicts;
- cap exposure by strategy, book, symbol, side, and account;
- apply fee/slippage profitability checks before approval;
- assign final margin, notional, leverage, stop, target, and ownership metadata;
- emit an auditable decision event for every accepted and rejected signal.

An accepted committee decision is an instruction to position management, not an
exchange order.

## Position Management Contract

Position management is the trader. It receives approved committee decisions and
converts them into position intents:

```text
open | add | reduce | close | reverse | hold
```

It must continuously monitor:

- entry ownership by strategy and committee decision;
- target and stop triggers;
- time stop and thesis invalidation;
- per-position worst-case loss at stop;
- account-level open risk;
- realized and unrealized PnL after fees and funding;
- duplicate entries and repeated signals;
- orphaned exchange positions from prior process failures;
- OKX reconciliation from positions, fills, and bills.

Minimum risk constraints:

- every opened position must have a stop or documented equivalent hard exit;
- stop-loss loss for one position must be capped against total account NAV;
- daily account loss and drawdown gates override strategy requests;
- flatten must verify exchange positions are actually closed;
- if reconciliation fails, new entries are blocked until account state is known.

Position management may close a position even without a new strategy signal if
stop, target, time, risk, or reconciliation rules require it.

## Execution And Accounting Contract

Execution management is the only layer that may call OKX order commands.
Strategies, committee code, and position management must emit intents, not raw
orders.

For OKX competition eligibility, live orders must go through Agent Trade Kit CLI
with `okx swap place` or an approved wrapper that calls it. Execution must:

- map symbols to `*-USDT-SWAP`;
- convert notional to OKX contract size;
- set profile by environment only: `competition -> live`, `personal -> personal`;
- prevent ambient environment credentials from overriding non-live profiles;
- dry-run where required by the operating procedure;
- record order id, fill id, fees, funding, slippage, and owner decision id.

Accounting reconciles internal state against OKX positions, bills, and fills.
Internal state is never authoritative by itself for live accounts.

## Environment Isolation

Personal and competition are different environments. The same strategy may not
run in both at the same time unless the Strategy Office explicitly registers two
separate strategy ids with separate state ids, risk budgets, and ownership.

Environment mapping:

```text
competition -> okx profile live
personal    -> okx profile personal
demo        -> okx profile demo
```

Every status page, pause button, flatten button, and runner operation must use
this mapping. If a profile cannot be verified, the system must fail closed.

## Required Runtime Health Signals

Every running strategy process must update:

```text
heartbeat_at
strategy_id
environment
state_id
runner_status
data_ready
feature_ready
last_signal_at
last_committee_submission_at
last_committee_decision_at
position_count
last_error
```

The control tower should distinguish:

- process alive;
- strategy ready;
- data stale;
- committee blocked;
- position manager blocked;
- execution/account reconciliation blocked.

`process alive` alone is not a healthy trading state.

## Migration Rule For Existing Code

Existing standalone scripts are tolerated only as implementation adapters behind
the runner. They are not architectural owners.

Examples:

- `scripts/run_c_auto_v2_micro_live.py` is an adapter for the `c_auto` strategy.
- `scripts/run_research_sleeve_paper.py` is an adapter for several research
  sleeve strategies.
- `launcher/launcher_server.py` currently contains the environment runner logic
  and must keep reading Strategy Office before starting anything.

Any new bug fix must preserve this boundary. If fixing a strategy requires
adding startup, account flattening, or direct order ownership inside the
strategy, the fix is architecturally wrong.
