# Code Review — Environment Isolation and Architecture

Date: 2026-04-24

Scope:
- Review how OKX API profiles flow through the bot.
- Ensure the three configured environments can be selected explicitly:
  - `demo`
  - `live` = competition
  - `personal`
- Identify broader architecture gaps that affect multi-strategy, multi-environment trading.

## High-Severity Findings Fixed

### 1. Custom strategy config ignored environment override

File: `engine/main.py`

Problem:
- `_run_custom_strategy()` built `effective_config`, applied `STRATEGY_PROFILE`, then called `mod.run(config=config, ...)`.
- Result: the override was printed but not actually passed into the strategy.

Fix:
- `mod.run(config=effective_config, ...)`.
- Added `--profile` to `python3 engine/main.py competition demo-start`.
- Validates profile against `~/.okx/config.toml`.

Impact:
- `python3 engine/main.py competition demo-start --strategy yolo_orchestrator --profile personal --foreground` now reaches the strategy as `profile=personal`.

### 2. Profile selection was hardcoded to `demo|live`

Files:
- `engine/main.py`
- `engine/competition/strategies/elite_flow.py`
- `engine/competition/strategies/yolo_momentum.py`
- `engine/competition/strategies/yolo_orchestrator.py`
- `start_local.sh`
- `manage_local.sh`

Problem:
- Code only accepted `demo` and `live`, so `personal` could not be selected cleanly.
- Some strategy constructors ignored any other `STRATEGY_PROFILE`.

Fix:
- Added validation against configured OKX profiles instead of hardcoding two names.
- `start_local.sh` and `manage_local.sh` now accept `personal`.
- `OKX_PROFILE` is exported alongside `STRATEGY_PROFILE`.

### 3. Standard Broker routed every non-demo account to `live`

Files:
- `engine/execution/broker.py`
- `engine/engine/trading_engine.py`
- `engine/main.py`

Problem:
- `Broker` used `profile = "demo" if sandbox else "live"` internally.
- A future standard-engine run intended for `personal` would silently route private calls to `live`.

Fix:
- `Broker(..., okx_profile=...)` now carries an explicit OKX profile.
- `TradingEngine(..., okx_profile=...)` passes it through.
- `main.py start` now supports `--okx-profile`.

Impact:
- The route from user command to ATK CLI profile is explicit.

### 4. State files could collide across environments

Files:
- `engine/competition/strategies/elite_flow.py`
- `engine/competition/strategies/yolo_momentum.py`
- `engine/competition/strategies/yolo_orchestrator.py`
- `start_local.sh`
- `stop_local.sh`

Problem:
- Some state files only split `live` vs everything else.
- `demo` and `personal` could share files or inherit stale state.
- PID files were strategy-only.

Fix:
- Standalone strategy state files now include the active profile.
- Local PID files now include strategy and environment.
- `stop_local.sh` cleans environment-specific PID files.

## Config Review

`~/.okx/config.toml` now exposes three profiles after fallback parsing:

- `demo`: credentials present, `demo=true`
- `live`: credentials present, `demo=false`
- `personal`: credentials present, `demo=false`

Important:
- `~/.okx/config.toml` was normalized from `api key` / `secret key` to `api_key` / `secret_key`.
- Best config format:

```toml
[profiles.demo]
api_key = "..."
secret_key = "..."
passphrase = "..."
demo = true

[profiles.live]
api_key = "..."
secret_key = "..."
passphrase = "..."
demo = false

[profiles.personal]
api_key = "..."
secret_key = "..."
passphrase = "..."
demo = false
```

## How To Run

Local wrapper:

```bash
./manage_local.sh start yolo_orchestrator 8080 demo
./manage_local.sh start yolo_orchestrator 8080 live
./manage_local.sh start yolo_orchestrator 8080 personal
```

Direct custom strategy:

```bash
python3 engine/main.py competition demo-start --strategy elite_flow --profile demo --foreground
python3 engine/main.py competition demo-start --strategy yolo_momentum --profile live --foreground
python3 engine/main.py competition demo-start --strategy yolo_orchestrator --profile personal --foreground
```

Standard engine:

```bash
python3 engine/main.py start --okx-profile demo --config '[{"id":"daily","strategy":"combined_portfolio","profile":"daily","capital":1000}]'
python3 engine/main.py start --okx-profile personal --live --config '[{"id":"daily","strategy":"combined_portfolio","profile":"daily","capital":1000}]'
```

## Remaining Architecture Risks

1. `logs/summary.json` is still a single dashboard surface.
   - It is fine for one active strategy at a time.
   - It is not enough for concurrent multi-environment execution.
   - Next step: write `summary_<profile>.json` and make dashboard accept `?profile=`.

2. Launchd scripts are still competition/live-specific.
   - `scripts/start_yolo_live.sh` and `scripts/start_yolo_orchestrator_live.sh` intentionally use `live`.
   - Personal environment should use `manage_local.sh` until launchd labels are made profile-specific.

3. Account-level risk is not centralized.
   - Each strategy has local guards.
   - There is no single pre-trade arbiter that can say: "this profile, this strategy, this order, this account state is allowed."

4. Feature store and label layer are still missing.
   - See `.claude/ARCHITECTURE_REVIEW_2026-04-24.md`.
   - This is the highest-leverage next architecture task.

5. `okx` CLI version is old.
   - Observed: `1.2.7`.
   - CLI reports newer `1.3.1`.
   - Pin or upgrade deliberately, then record behavior changes.

## Verification

Commands run:

```bash
python3 -m py_compile ...
zsh -n start_local.sh
zsh -n stop_local.sh
zsh -n manage_local.sh
python3 engine/main.py competition demo-start --help
./manage_local.sh --help
```

Profile discovery check returned:

```text
profiles = ['demo', 'live', 'personal']
demo: credentials present
live: credentials present
personal: credentials present
```

Private endpoint smoke check:
- `okx --profile live --json account config`: OK, label `okx_trading_ai_skill`, `posMode=net_mode`.
- `okx --profile personal --json account config`: OK, label `trading_skill_test`, `posMode=net_mode`.
- `okx --profile demo --json account config`: failed with `HTTP 401 Invalid OK-ACCESS-KEY`; profile fields are present, but the demo credential itself appears invalid/stale for the OKX CLI.
