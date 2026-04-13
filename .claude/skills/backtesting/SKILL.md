---
name: backtesting
description: Interactively backtest one or more competition strategies with capital allocation, optional 14-day chunked analysis, and a randomness/edge verdict.
---

# Backtesting Skill

Run strategy backtests with capital allocation, optional chunked analysis, and randomness assessment.

## Workflow

### Step 1 — Select strategies

Read `.claude/knowledge/strategies/_index.md` to get the current strategy list and their statuses.

Present the user with a numbered menu of available strategies (include ID, name, status, and latest return if known). Ask:

> Which strategy/strategies would you like to backtest? Enter numbers separated by commas (e.g. 1, 2) or "all".

Wait for the user's answer before continuing.

### Step 2 — Capital allocation

For each selected strategy, ask:

> How much capital (USDT) would you like to allocate to **{strategy name}** for this backtest?

If multiple strategies are selected, ask for all allocations upfront in one message (list them). Inform the user that allocations affect fee estimates and position sizing in the report — they do NOT place real orders.

Wait for the user's answer before continuing.

### Step 3 — Chunking mode

Explain the two modes, then ask the user to choose:

> **Mode A — Chunked (recommended for robustness check)**
> Splits the full period (2025-01-01 → 2026-03-31, ~15 months) into consecutive 14-day windows. Each chunk is backtested independently and results are compared across chunks to detect whether performance is consistent or driven by a lucky few periods.
>
> **Mode B — Full period**
> Runs one backtest over the entire 2025-01-01 → 2026-03-31 range. Good for headline metrics (total return, Sharpe, max DD) but can mask regime-dependence.
>
> Which mode? (A / B)

Wait for the user's answer before continuing.

### Step 4 — Run backtests

#### Mode B (full period)
For each selected strategy run:
```bash
python3 .claude/tools/backtest_strategy.py {strategy_id} --start 2025-01-01 --end 2026-03-31
```
If the user specified a non-default capital, note it in the analysis but the tool itself uses the config capital — flag any discrepancy to the user.

#### Mode A (chunked)
Generate 14-day windows covering 2025-01-01 → 2026-03-31:
- 2025-01-01 → 2025-01-14
- 2025-01-15 → 2025-01-28
- 2025-01-29 → 2025-02-11
- … continue until 2026-03-31 (last chunk may be shorter)

For each strategy, run the backtest tool once per chunk:
```bash
python3 .claude/tools/backtest_strategy.py {strategy_id} --start {chunk_start} --end {chunk_end}
```

Run all chunks for a strategy sequentially (each run needs the previous to complete). If there are multiple strategies, run each strategy's full chunk suite before moving to the next.

### Step 5 — Analysis & verdict

#### For Mode B
Present a single results table per strategy:

| Metric | Value |
|--------|-------|
| Total return | |
| Sharpe ratio | |
| Max drawdown | |
| Win rate | |
| Trade count | |
| Total fees | |

Then give a qualitative verdict on randomness — see criteria below.

#### For Mode A (chunked analysis)

Build a chunk summary table:

| Chunk | Start | End | Return | Sharpe | Max DD | Win Rate | Trades |
|-------|-------|-----|--------|--------|--------|----------|--------|

Then compute and display:
- **Positive chunk rate**: % of chunks with positive return
- **Return std dev across chunks**: high = erratic
- **Sharpe consistency**: std dev of per-chunk Sharpe
- **Win rate range**: min → max across chunks

#### Randomness verdict criteria

Use the following rubric to classify each strategy:

| Signal | Weight | Interpretation |
|--------|--------|----------------|
| Sharpe > 0.5 consistently | Strong | Genuine edge |
| Positive chunk rate ≥ 65% | Strong | Edge present most regimes |
| Positive chunk rate 45–65% | Moderate | Weak/regime-dependent edge |
| Positive chunk rate < 45% | Weak | Likely random or mean-reverting noise |
| Return std dev > 3× mean return | Weak | High variance, luck-sensitive |
| Win rate < 45% | Weak | Adverse selection or random entry |
| Max DD > 2× total return | Weak | Risk-adjusted edge is poor |

Conclude with one of:
- **ROBUST** — Multiple strong signals. Likely genuine alpha.
- **MARGINAL** — Mixed signals. Consider further testing or parameter review.
- **RANDOM** — Majority weak signals. Do not deploy without significant rework.

#### Multi-strategy comparison (if >1 strategy selected)
Add a final side-by-side comparison table ranking strategies by Sharpe and positive chunk rate. Recommend which strategy (or combination) to deploy for the competition window.

### Step 6 — Update knowledge base

After analysis, offer:
> Would you like me to save these backtest results to the strategy knowledge base? (yes / no)

If yes, update `.claude/knowledge/strategies/_index.md` (Backtest Results Archive table) and the relevant `.claude/knowledge/strategies/{id}.md` file with the new run date, period, and metrics.
