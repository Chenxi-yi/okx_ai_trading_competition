# Donchian Breakout Strategy Research

Last updated: 2026-05-13

## Idea

User idea:

- Break above the prior 20-day high: go long.
- Break below the prior 20-day low: go short.

This is a classic Donchian channel / trend-following breakout. The first research implementation uses daily bars, confirms the breakout on daily close, and enters at the next daily open to avoid look-ahead bias.

Research script:

```bash
python3 scripts/research_donchian_breakout.py \
  --symbol-source cache \
  --start 2025-01-01 \
  --max-symbols 120
```

Output root:

`engine/data/research/donchian_breakout/`

The script now supports a broader key-level breakout family:

- `--level-kind donchian`: prior N-day high/low.
- `--level-kind swing`: confirmed swing pivot high/low, with Donchian fallback.
- `--entry-mode close`: daily close breakout, enter next daily open.
- `--entry-mode retest`: daily close breakout, wait for a retest/hold before entry.
- `--side-filter long|short|both`: isolate one side.

## 2026-05-13 Results

Costs include 5 bps fee per side and 2 bps slippage per side.

| Run | Trades | Symbols | Win | Mean net | Positive months | Recent since 2026-04-01 | Verdict |
|---|---:|---:|---:|---:|---:|---|---|
| `donchian_20d_baseline` | 354 | 68 | 41.24% | +0.807% | 50.00% | 65 trades, 35.38% win, -0.175% mean | Marginal / not deployable |
| `donchian_20d_trend_vol` | 251 | 60 | 41.04% | +1.080% | 56.25% | 47 trades, 36.17% win, -0.151% mean | Marginal / not deployable |
| `donchian_20d_fast_filtered` | 787 | 86 | 45.24% | +0.444% | 43.75% | 123 trades, 38.21% win, -0.821% mean | Random |
| `donchian_20d_slow_trend` | 67 | 28 | 50.75% | +3.995% | 53.33% | 14 trades, 35.71% win, -2.153% mean | Marginal / sample too small |

Best-looking slow trend setup:

```bash
python3 scripts/research_donchian_breakout.py \
  --symbol-source cache \
  --start 2025-01-01 \
  --max-symbols 120 \
  --trend-filter sma120 \
  --stop-atr 2.5 \
  --target-r 2.5 \
  --max-hold-days 15 \
  --out-id donchian_20d_slow_trend
```

By side for the slow trend setup:

| Side | Trades | Win | Mean net | Sum net |
|---|---:|---:|---:|---:|
| Long | 28 | 39.29% | +0.853% | +23.89% |
| Short | 39 | 58.97% | +6.251% | +243.81% |

Recent weakness remains the main problem: 2026-04 and 2026-05 were both negative in the slow trend run.

## Interpretation

The raw idea is directionally reasonable from first principles: a 20-day high/low break measures cross-day range expansion and can catch persistent trend continuation.

The current evidence is not strong enough for live or default paper deployment:

- Baseline is profitable historically, but median trade is negative and win rate is only about 41%.
- The best-looking variant is heavily helped by a few large short wins around 2026-01.
- Recent post-2026-04 behavior is negative across the tested variants.
- Short side is materially better than long side in the slow-trend setup, but sample size is small.

Recommended next step:

- Do not add as an autonomous paper sleeve yet.
- Reframe as a gate or weak feature for existing strategy selection:
  - allow longs only when price is near or above the 20-day upper channel and higher-timeframe trend is supportive;
  - allow shorts only when price breaks the 20-day lower channel and rebound momentum is not strong;
  - avoid taking raw breakout entries immediately after broad-market exhaustion candles.

## 2026-05-13 Key-Level Family Probe

First-principles view:

- Breakout is a valid research family because it measures supply/demand imbalance at a visible level.
- A clean break can trigger stop orders, trend-following orders, and delayed information repricing.
- The edge should be regime-dependent; it is expected to work better in expansion/trending phases and fail in mean-reverting chop.

Targeted family tests after expanding the script:

| Run | Level | Entry | Side | Trades | Win | Mean net | Positive months | Recent since 2026-04-01 | Verdict |
|---|---|---|---|---:|---:|---:|---:|---|---|
| `breakout_donchian10_close_trend_vol` | 10d high/low | close | both | 297 | 44.78% | +1.021% | 43.75% | 51 trades, 47.06% win, +1.000% mean | Recent useful, long-led |
| `breakout_donchian30_close_trend_vol` | 30d high/low | close | both | 170 | 40.00% | +0.631% | 66.67% | 27 trades, 40.74% win, -0.125% mean | Not useful recently |
| `breakout_donchian55_close_slow_vol` | 55d high/low | close | both | 111 | 40.54% | +0.702% | 53.33% | 15 trades, 40.00% win, -2.135% mean | Short-biased, stale |
| `breakout_donchian90_close_slow_vol` | 90d high/low | close | both | 56 | 44.64% | +2.106% | 38.46% | 8 trades, 37.50% win, -1.492% mean | Too sparse |
| `breakout_donchian10_long_close_trend_vol` | 10d high | close | long | 158 | 44.94% | +1.256% | 33.33% | 46 trades, 58.70% win, +3.569% mean | Best current regime candidate |
| `breakout_donchian10_short_close_trend_vol` | 10d low | close | short | 166 | 42.17% | +0.327% | 53.33% | 9 trades, 11.11% win, -6.621% mean | Avoid recently |
| `breakout_swing10_long_retest_trend_vol` | swing high | retest | long | 71 | 39.44% | -1.584% | 25.00% | 27 trades, 62.96% win, +3.566% mean | Recent useful, poor history |
| `breakout_swing10_short_retest_trend_vol` | swing low | retest | short | 94 | 51.06% | +2.091% | 46.67% | 15 trades, 20.00% win, -7.130% mean | Historical short trap |

Interpretation update:

- The interesting pocket is not "all breakouts"; it is currently **short-window upside breakout**.
- Short breakdown systems looked good around 2026-01 but have recently become dangerous.
- Longer window breakouts are too sparse or stale for this account size.
- Retest entries improve the intuition but do not solve regime dependence.

Recommended next step:

- Add a non-trading feature/gate first:
  - `donchian10_up_breakout_recent`: positive vote for long candidates during expansion regimes.
  - `donchian10_down_breakout_recent`: do not auto-short; use only as a warning/gate unless broader market is also weak.
- If moved to paper later, start long-only, low risk budget, and require additional confirmation from existing committee signals.
