# Full Refactor Blueprint

Last updated: 2026-05-18T15:05:00+08:00

This is the implementation blueprint for the architecture in
`.claude/knowledge/runtime_strategy_governance.md`. A new conversation should
start here before changing trading code.

## Goal

Move the system from script-driven trading to a registry-driven, modular
personal quant engine:

```text
Strategy Office
  -> Environment Runner
  -> Data Readiness
  -> Strategy Adapter
  -> Investment Committee
  -> Position Manager
  -> Risk Manager
  -> Execution Router
  -> Accounting / Reconciliation
  -> Control Tower
```

The immediate objective is not to rewrite every strategy. It is to make this
chain the only valid runtime path, then migrate old scripts behind adapters.

## Module Ownership

| Module | Owns | Must Not Own |
|---|---|---|
| Strategy Office | strategy identity, status, params, runtime envs, data dependencies, risk budget | process spawning, orders |
| Environment Runner | start/stop supervised strategy processes based on registry | signal logic, order sizing alpha |
| Data Readiness | freshness and coverage checks for declared datasets/features | strategy-specific hidden downloads |
| Strategy Adapter | converts legacy strategy implementation into normalized signals/candidates | account state authority, exchange orders |
| Investment Committee | accepts/rejects candidates, resolves conflicts, assigns approved trade plan | exchange execution |
| Position Manager | open/add/reduce/close/hold lifecycle, stop/target/time-stop, flatten intent | raw OKX calls |
| Risk Manager | account, strategy, symbol, drawdown, loss-at-stop gates | alpha generation |
| Execution Router | OKX Agent Trade Kit commands, contract sizing, order submission | deciding whether alpha is good |
| Accounting/Reconciliation | fills, bills, positions, NAV, ownership truth | alpha generation, registry status |
| Control Tower | status, buttons, logs, alerts | direct strategy bypass |

If code seems to belong to two modules, split the decision object from the
execution object. For example, a stop price belongs to the committee/position
plan; submitting a stop order belongs to execution.

## Refactor Phases

### Phase 1: Lock The Contract

- Add runtime and data dependency fields to Strategy Office schema.
- Add a canonical environment runner module.
- Make docs and `AGENTS.md` point to the architecture constraints.
- Keep live trading behavior unchanged while introducing the new path.

### Phase 2: Runner Becomes The Only Start Path

- Frontend Start Personal / Start Competition calls the environment runner.
- Runner reads Strategy Office and starts all allowed strategies.
- Direct strategy start endpoints either delegate to the runner or are disabled.
- Runner writes structured health for each layer:
  process, data, strategy, committee, position, execution, reconciliation.

### Phase 3: Strategies Become Signal Adapters

- Move live order calls out of legacy strategy scripts.
- Legacy scripts may remain as adapters only if they emit normalized
  `Signal` or `CandidateTrade`.
- `c_auto`, US-equity sleeves, trend sleeves, and swing sleeves all share the
  same committee and position manager path.

### Phase 4: Position Manager Owns Trading Lifecycle

- Stop/target/time-stop/flatten/external-exit logic moves out of strategy
  scripts and launcher helpers.
- Flatten returns only after OKX positions reconcile flat.
- New entries are blocked when reconciliation cannot verify account state.

### Phase 5: Accounting Becomes Authoritative

- Live state is reconstructed from OKX positions, bills, fills, and internal
  ownership journals.
- JSON state files are caches, not truth.
- Dashboard displays stale/internal/OKX-disagreed states distinctly.

## Migration Rules

1. No new direct `okx` calls outside execution, reconciliation, or read-only
   account probes.
2. No strategy may call `_place_live_entry`, `swap place`, or a Broker directly.
3. No frontend endpoint may start one strategy unless it delegates to registry
   policy and environment runner checks.
4. A strategy can be live only if `runtime.enabled`, environment allowed,
   `live_enabled`, risk budget, data dependencies, and parameter set all pass.
5. Any bug fix that adds more behavior to a strategy script must be challenged.
   Prefer moving behavior into committee, position, risk, execution, or
   accounting.

## Current Known Debt

- `launcher/launcher_server.py` still contains runner, account control,
  attribution, and dashboard API logic in one file.
- `scripts/run_research_sleeve_paper.py` still owns some live position checks,
  entry/exit logic, and OKX order calls.
- `scripts/run_c_auto_v2_micro_live.py` still mixes strategy, committee,
  position, and execution behavior.
- Strategy Office JSON has runtime fields, but the dataclass schema did not
  previously enforce them.
- Dashboard can read stale files unless freshness and process truth are both
  checked.

## Completion Definition

The refactor is complete when:

- starting an environment never names an individual strategy in frontend code;
- pausing an environment stops all registry-selected processes and reports
  truth from process checks;
- flattening an environment closes and reconciles actual OKX account positions;
- all live entries originate from committee-approved decisions;
- position exits are generated by Position Manager or account risk;
- live status distinguishes alive, ready, blocked, stale, and reconciled states;
- a new strategy can be added by registering data dependencies, params, risk,
  runtime, and an adapter, without changing launcher control flow.
