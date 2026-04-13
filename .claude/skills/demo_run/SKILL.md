---
name: demo_run
description: Interactively start a competition strategy demo run — asks strategy, capital to deploy, then launches in foreground so the terminal stays open.
---

# Demo Run Skill

Launch a competition strategy in demo mode with a live terminal.

## Workflow

### Step 1 — Select strategy

Read `.claude/knowledge/strategies/_index.md` to get the current strategy list.

Present the user with a numbered menu (include ID, name, status, and profile):

> Which strategy would you like to run?
> 1. elite_flow — Meridian (daily, ✅ Active)
> 2. elite_flow — Velocity (hourly, 🔬 Testing)
> 3. elite_flow — Elite Alpha (tick/1min, ✅ Built)

Wait for the user's answer before continuing. Only one strategy can run at a time — if the engine is already running, tell the user to stop it first with `python3 .claude/tools/stop_engine.py`.

### Step 2 — Capital to deploy

Ask:

> How much capital (USDT) would you like to deploy for this run?
> Current configured capital for **{strategy name}**: {current_capital} USDT

Inform the user:
- Capital is configured in `engine/config/competition_strategies.json` under `current_capital`
- If they specify a different amount, update `current_capital` in that file before launching
- Do NOT add capital beyond the seed unless the user explicitly authorises it (dilutes ROI denominator)

Wait for the user's answer. If the amount differs from `current_capital`, update the JSON file:
```python
# Read engine/config/competition_strategies.json
# Find the strategy entry by id
# Set current_capital = <user's amount>
# Write back
```

### Step 3 — Pre-flight checks

Run these checks silently and only surface failures:

1. **Engine not already running:**
```bash
python3 .claude/tools/trading_status.py
```
If running, stop here and tell the user: "Engine is already running. Run `python3 .claude/tools/stop_engine.py` first."

2. **Position mode check (for elite_flow only):**
```bash
okx --profile demo account config --json
```
If `posMode` is not `net`, run:
```bash
okx --profile demo account set-position-mode net
```
Confirm it succeeded before continuing.

3. **Balance check:**
```bash
python3 .claude/tools/check_balance.py
```
If available balance < configured capital, warn the user but don't block.

### Step 4 — Launch

Tell the user:

> Launching **{strategy name}** in foreground mode. The terminal will stay open showing live output.
> Press Ctrl+C to stop.

Then run — **this is the exact command to give the user to run themselves**, since it needs an interactive terminal:

```
python3 .claude/tools/start_demo.py {strategy_id} --foreground
```

**IMPORTANT:** Do NOT use the Bash tool to run this command — it will block and the user won't see live output. Instead, display the command and instruct the user to run it themselves by typing `! python3 .claude/tools/start_demo.py {strategy_id} --foreground` in the prompt (the `!` prefix pipes it to their terminal).

Format the instruction clearly:

> Run this in your terminal:
> ```
> ! python3 .claude/tools/start_demo.py {strategy_id} --foreground
> ```
> The `!` prefix runs it in your shell session so you'll see the live output here.

### Step 5 — After launch

Once the user has run the command, check status directly from logs — do NOT ask the user to paste terminal output:

```bash
tail -50 engine/logs/elite_flow.log          # or engine/logs/engine.log for other strategies
python3 .claude/tools/check_positions.py
python3 .claude/tools/trading_status.py
```

Report what you see: warmup progress, signals fired, orders placed, any errors. Offer to monitor periodically using the `/loop` skill.
