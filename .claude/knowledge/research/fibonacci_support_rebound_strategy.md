# Fibonacci Support Rebound Research

## Thesis

Use 4h structure to estimate Fibonacci retracement support, then enter long only after the support is touched and reclaimed.

This is a rebound strategy, not a trend-following entry. It should not buy every retracement. Research indicates the shallow/mid retracement levels are too noisy, while the deep 0.786 retracement has some positive expectancy but weak consistency.

## Data And Cost Assumptions

- Source: local OKX swap OHLCV cache under `engine/data/cache`.
- Research script: `scripts/research_fibonacci_support_rebound.py`.
- Costs: 5 bps fee plus 2 bps slippage per side, deducted on every simulated round trip.
- Long-only first pass.
- 4h version uses 4h bars for both structure and execution so it can use a longer cache history. 1h cache is currently too short for many symbols and should not be used alone for a cross-cycle verdict.

## Main Results

Baseline 4h all-level test:

```bash
python3 scripts/research_fibonacci_support_rebound.py \
  --out-id fib_support_rebound_4h_probe_v1 \
  --symbol-source cache \
  --entry-timeframe 4h \
  --start 2025-01-01 \
  --max-symbols 160 \
  --support-tolerance-pct 0.004 \
  --max-pierce-pct 0.012 \
  --min-impulse-pct 0.05 \
  --ratios 0.5,0.618,0.786 \
  --target-r 1.8 \
  --confirm-mode reclaim \
  --max-hold-hours 24
```

| Run | Trades | Win | Mean net | Positive months | Recent since 2026-04-01 | Verdict |
|---|---:|---:|---:|---:|---:|---|
| `fib_support_rebound_4h_probe_v1` | 4,327 | 40.12% | -0.024% | 52.94% | 811 trades, +0.112% mean | Random |

By level:

| Level | Trades | Win | Mean net |
|---|---:|---:|---:|
| 0.5 | 2,238 | 39.99% | -0.046% |
| 0.618 | 1,750 | 39.14% | -0.033% |
| 0.786 | 339 | 46.02% | +0.171% |

Deep 0.786-only test:

| Run | Trades | Win | Mean net | Positive months | Recent since 2026-04-01 | Verdict |
|---|---:|---:|---:|---:|---:|---|
| `fib_support_rebound_4h_deep786_v1` | 344 | 46.51% | +0.191% | 52.94% | 90 trades, +0.459% mean | Random |
| `fib_support_rebound_4h_deep786_strong_v1` | 267 | 44.57% | +0.106% | 52.94% | 65 trades, +0.293% mean | Random |
| `fib_support_rebound_4h_deep786_impulse12_v1` | 261 | 42.91% | +0.054% | 37.50% | 61 trades, +0.471% mean | Random |
| `fib_support_rebound_4h_deep786_tight_v1` | 203 | 45.81% | -0.034% | 47.06% | 49 trades, -0.010% mean | Random |
| `fib_support_rebound_4h_deep786_hold48_v1` | 302 | 42.72% | +0.119% | 47.06% | 72 trades, +0.568% mean | Random |

## Interpretation

- The first-principles idea is plausible: deep retracement support can define asymmetric rebound trades.
- Empirically, shallow and mid Fibonacci supports are not enough. They mostly catch falling knives and fee-adjusted expectancy is negative.
- The 0.786 level is the only level with positive average net return, but monthly consistency is too weak for standalone deployment.
- Recent market behavior is better than full history, but that also makes the result regime-sensitive. Do not promote directly to paper/live based on this alone.

## Recommended Use

Use as a feature or gate:

- Allow rebound longs only near deep 0.786 4h support.
- Require an additional independent signal before entry, such as trend-pullback reversal, smart-money diffusion, orderbook absorption, or BTC regime confirmation.
- Avoid treating 0.5 and 0.618 supports as long entries by themselves.
- If promoted to paper later, run as a low-priority sleeve with max one concurrent position and require at least 50 paper trades before live consideration.

## Daily Support Extension

Tested daily structure with 4h execution because the 1h cache is too short for a cross-cycle verdict. The daily version uses a 60-day rolling high/low, computes the 0.786 retracement support, then waits for 4h reclaim confirmation.

Best current candidate:

```bash
python3 scripts/research_fibonacci_support_rebound.py \
  --out-id fib_support_rebound_daily_786_tight_v2 \
  --symbol-source cache \
  --structure-timeframe 1d \
  --entry-timeframe 4h \
  --start 2025-01-01 \
  --max-symbols 160 \
  --lookback-structure-bars 60 \
  --trend-sma-structure 40 \
  --support-tolerance-pct 0.004 \
  --max-pierce-pct 0.012 \
  --min-impulse-pct 0.12 \
  --ratios 0.786 \
  --target-r 1.8 \
  --confirm-mode reclaim \
  --max-hold-hours 48
```

| Run | Structure | Entry | Trades | Symbols | Win | Mean net | Positive months | Recent since 2026-04-01 | Verdict |
|---|---|---|---:|---:|---:|---:|---:|---:|---|
| `fib_support_rebound_daily_4h_probe_v1` | 1d 0.5/0.618/0.786 | 4h | 1,523 | 114 | 44.25% | +0.267% | 82.35% | 421 trades, +0.325% mean | Random |
| `fib_support_rebound_daily_786_v1` | 1d 0.786 | 4h | 292 | 99 | 52.74% | +0.748% | 80.00% | 64 trades, +0.802% mean | Marginal |
| `fib_support_rebound_daily_786_strong_v1` | 1d 0.786 | 4h strong reclaim | 258 | 96 | 50.39% | +0.594% | 80.00% | 57 trades, +0.627% mean | Marginal |
| `fib_support_rebound_daily_786_tight_v2` | 1d 0.786 tight touch | 4h | 225 | 87 | 55.11% | +0.840% | 80.00% | 53 trades, +0.717% mean | Robust |
| `fib_support_rebound_daily_786_impulse20_v1` | 1d 0.786 higher impulse | 4h | 291 | 99 | 52.92% | +0.755% | 86.67% | 64 trades, +0.802% mean | Marginal |

Monthly distribution for `fib_support_rebound_daily_786_tight_v2`:

- Positive: 2025-03, 2025-04, 2025-06, 2025-07, 2025-08, 2025-09, 2025-10, 2025-12, 2026-01, 2026-03, 2026-04, 2026-05.
- Negative: 2025-05, 2025-11, 2026-02.

Interpretation:

- Daily 0.786 support is materially better than 4h support as the anchor.
- The edge appears to come from deep daily retracement plus 4h confirmation, not from Fibonacci levels generally.
- This is a candidate for paper as an isolated long-only sleeve, but it still needs live-data/paper verification because the execution model assumes bar-level stop/target fills.

Candidate paper constraints:

- Long-only.
- Daily 0.786 support only; do not trade 0.5/0.618.
- 4h reclaim confirmation required.
- Max one concurrent position from this sleeve.
- Per-trade risk still capped by global <= 2% equity rule; size by stop distance.
- Require at least 50 paper trades, win >= 52%, mean net > 0 after actual fees/slippage, and no environment/risk breach before live consideration.
