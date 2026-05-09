# Smart-Money Diffusion Strategy Research

Last updated: 2026-05-09

## Thesis

Treat OKX smart-money data as an independent strategy source, not as a copy
trading shortcut and not initially as an investment-committee veto.

The research question is whether smart-money position diffusion leads price:

- early adoption: a small number of qualified traders starts holding a coin;
- diffusion: traders with position, total notional, or net notional expands;
- exit: traders with position or total notional contracts;
- price response: forward returns after those transitions.

Smart money is not assumed to be smart. The edge must come from measured
lead-lag behavior after publication delay and fees.

## Kit source

After upgrading OKX CLI to 1.3.3, smart-money commands changed to:

```text
okx smartmoney signal-overview-by-filter
okx smartmoney signal-trend-by-filter
okx smartmoney signal-overview-by-trader
okx smartmoney signal-trend-by-trader
okx smartmoney traders-by-filter
okx smartmoney trader-positions
okx smartmoney trader-positions-history
okx smartmoney trader-orders-history
```

For signal history, use:

```bash
okx smartmoney signal-trend-by-filter \
  --instCcy NOT \
  --asOfTime 2026050905 \
  --granularity 1h \
  --limit 24 \
  --period 7 \
  --lmtNum 100 \
  --json
```

`asOfTime` is `yyyyMMddHH` UTC.

## Current fields

Useful fields observed:

- `tradersWithPosition`
- `longTraders`, `shortTraders`
- `longRatio`, `shortRatio`
- `weightedLongRatio`, `weightedShortRatio`
- `netNotionalUsdt`
- `totalNotionalUsdt`
- `tradersQualified`

Overview additionally includes:

- `totalNotionalVs24h`
- `smartMoneyLongAvgEntry`
- `smartMoneyShortAvgEntry`
- average long/short win rate

## First probe

Script:

```bash
python3 scripts/research_smartmoney_diffusion.py \
  --symbols NOT,FIL,AR \
  --as-of 2026050905 \
  --limit 24
```

Report:

```text
engine/research/reports/smartmoney_diffusion/run_2026050905_20260509_090740/smartmoney_diffusion_report.md
```

Initial sample is tiny and not tradable yet, but directionally interesting:

- `long_diffusion_event`: 9 samples, avg forward 6h return about +1.27%.
- `long_exit_event`: 5 samples, avg forward 6h return about -3.42%.
- NOT was strongly smart-money long while C-Auto opened short; this is a good
  case study for strategy research, but not enough for production rules.

## Expanded probe

Script:

```bash
python3 scripts/research_smartmoney_diffusion.py \
  --symbols auto \
  --max-symbols 80 \
  --as-of 2026050905 \
  --limit 72
```

Report:

```text
engine/research/reports/smartmoney_diffusion/run_2026050905_20260509_091456/smartmoney_diffusion_report.md
```

Coverage:

- requested 80 smart-money overview names;
- 67 symbols returned usable trend rows;
- 3583 symbol-hour rows after joining local 1h OHLCV cache.

Event-study results:

| event | samples | avg fwd 6h | avg fwd 12h | avg fwd 24h |
|---|---:|---:|---:|---:|
| long diffusion | 392 | +0.37% | +0.45% | +1.04% |
| long exit | 303 | +0.63% | +1.32% | +1.77% |
| short diffusion | 159 | +0.08% | -0.21% | +0.06% |
| short exit | 148 | +0.13% | +0.16% | +0.11% |
| all smart-money long | 3071 | +0.53% | +1.07% | +1.71% |
| all smart-money short | 512 | -0.31% | -0.25% | -0.22% |

Interpretation:

- Naked copy-trading is not justified by this probe.
- Smart-money long exposure has mild positive drift in this sample, but long
  exit is not bearish in aggregate. This means exit rules need more context
  such as liquidity, OI, funding, recent price extension, and trader count.
- Smart-money short exposure is more interesting as a bearish/avoidance signal:
  `all_smartmoney_short` has negative average forward returns from 3h to 24h.
- Small-count names can dominate ratios. Minimum trader/notional filters are
  mandatory before this becomes a candidate strategy.

Quick robustness filters on the same panel:

| filter | samples | symbols | avg fwd 6h | avg fwd 12h | avg fwd 24h |
|---|---:|---:|---:|---:|---:|
| net long, >=3 traders, >=50k notional | 591 | 15 | +0.50% | +0.73% | +0.76% |
| long diffusion, >=3 traders, >=50k notional | 173 | 15 | +0.48% | -0.08% | -0.33% |
| weighted long >=80%, net long, >=3 traders, >=50k notional | 452 | 14 | +0.69% | +1.15% | +1.24% |
| net short, >=3 traders, >=50k notional | 251 | 10 | +0.16% | +0.52% | +0.45% |
| weighted short >=80%, net short, >=3 traders, >=50k notional | 122 | 4 | -0.16% | -0.56% | -0.85% |

Current best hypothesis:

- Long side: strong weighted-long exposure with minimum participation/notional is
  a better candidate than raw diffusion.
- Short side: strong weighted-short exposure may be useful as an avoid/short
  signal, but sample breadth is still small.
- "Exit" is not universally bearish. Exit needs regime and price-extension
  context before it can be used.

## Candidate strategy prototype

Script:

```bash
python3 scripts/backtest_smartmoney_consensus.py
```

Rule:

- long if `weightedLongRatio >= threshold`, `netNotionalUsdt > 0`,
  `tradersWithPosition >= 3`, and `totalNotionalUsdt >= threshold_notional`;
- short if `weightedShortRatio >= threshold`, `netNotionalUsdt < 0`,
  `tradersWithPosition >= 3`, and `totalNotionalUsdt >= threshold_notional`;
- fixed 117U notional, max 4 concurrent positions by default;
- round-trip cost = 14 bps.

Prototype results on the 67-symbol / 3583-row panel:

| run | return | max DD | trades | win rate | note |
|---|---:|---:|---:|---:|---|
| 6h hold, threshold 0.8 | +1.33% | -0.65% | 47 | 51.06% | more trades, noisier |
| 12h hold, threshold 0.8 | +1.85% | -0.44% | 23 | 65.22% | baseline prototype |
| 24h hold, threshold 0.8 | +1.60% | -0.21% | 12 | 58.33% | low sample count |
| 12h hold, threshold 0.7 | +1.51% | -0.45% | 23 | 65.22% | lower threshold not better |
| 12h hold, threshold 0.9 | +0.20% | -0.27% | 23 | 65.22% | too strict/late |
| 12h hold, max 2 positions | +1.98% | -0.31% | 11 | 72.73% | better but low sample |
| 12h hold, min notional 200k | +2.29% | -0.46% | 23 | 69.57% | best probe |

This is promising but still not production evidence. The sample window is short
and some PnL is concentrated in a few names such as LAB/RAVE. Next requirement:
run the collector over multiple days and repeat the test as walk-forward rather
than tuning on one 72-hour panel.

## Next experiments

1. Collect a larger 1h universe for the top 50-100 USDT swaps.
2. Build event labels:
   - long diffusion
   - short diffusion
   - long exit
   - short exit
   - crowded late long
   - crowded late short
3. Test forward returns at 1h, 3h, 6h, 12h, 24h.
4. Compare against liquidity, OI, funding, and news sentiment.
5. Convert only robust effects into a standalone candidate strategy.
