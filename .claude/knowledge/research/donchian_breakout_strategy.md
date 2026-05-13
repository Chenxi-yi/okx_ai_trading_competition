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
