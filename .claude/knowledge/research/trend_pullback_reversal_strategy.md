# Trend Pullback Reversal Strategy

## Thesis

顺大逆小：4h defines the directional bias, 1h waits for a controlled countertrend pullback, then enters when the pullback shows a reversal trigger near the local extreme.

The first-pass feature-proxy research uses `c_auto_feature_store_rebuild_161_ohlcv_snapshot_v1` and subtracts 5 bps fee plus 2 bps slippage per side. It is research-only; no live order path uses this yet.

## Current Research Verdict

Best deployable candidate is long-only in bullish/constructive regimes:

```bash
python3 scripts/research_trend_pullback_reversal.py \
  --out-id trend_pullback_reversal_long_strong_v1 \
  --side-mode long \
  --regime-allowlist bull,chop_long,strong_bull \
  --h4-trend-min 0.012 \
  --h4-countertrend-allow 0.005 \
  --near-extreme-pct 0.0015 \
  --loose-extreme-pct 0.003 \
  --max-countertrend-multiple 4 \
  --max-hold-hours 6
```

Result:

| Run | Trades | Win | Mean net | Positive months | Recent since 2026-04-01 |
|---|---:|---:|---:|---:|---:|
| `trend_pullback_reversal_long_strong_v1` | 3,415 | 57.48% | +0.608% | 86.67% | 361 trades, 58.17% win, +0.812% mean |

## Sweep Findings

Quick sweep output: `engine/data/research/trend_pullback_reversal/trend_pullback_reversal_sweep_quick_v2/`.

The best overall score was long-only with stricter 4h trend:

| Side | h4 trend min | Hold | Trades | Win | Mean net | Recent mean |
|---|---:|---:|---:|---:|---:|---:|
| long | 1.2% | 12h | 7,553 | 53.97% | +0.533% | +1.119% |
| long | 1.2% | 6h | 5,852 | 55.38% | +0.516% | +0.690% |

Short had strong full-history stats, but recent behavior degraded:

| Short Filter | Trades | Full Win | Full Mean | Since 2026-04-01 |
|---|---:|---:|---:|---:|
| unrestricted quick top | 9,806 | 58.46% | +0.665% | 553 trades, 49.37% win, +0.058% mean |
| `bear,chop_short,deep_bear` | 5,990 | 54.91% | +0.555% | 92 trades, 32.61% win, -0.564% mean |
| `bear,deep_bear` | 4,714 | 53.75% | +0.488% | 75 trades, 33.33% win, -0.367% mean |

## Deployment Gate

Do not promote short side until recent paper recovers. Candidate paper gate:

- Start with long-only, bullish/constructive regime allowlist.
- Max one concurrent position from this sleeve until 50 paper trades.
- Require at least 50 paper trades, win >= 53%, mean net return > 0 after actual fee/slippage, and no rule breach before live consideration.
- Keep existing global risk: per-trade loss <= 2% equity; daily loss >= 6% equity triggers 24h real-trade cooldown.
