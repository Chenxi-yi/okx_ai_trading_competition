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

