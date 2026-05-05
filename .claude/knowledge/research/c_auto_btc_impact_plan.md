# C-Auto BTC Impact Study Plan

Created: 2026-05-05T17:12:00+0800
Status: completed
Experiment ID: `btc_alt_impact_v1`

## Objective

Before defining C-Auto bull/bear regimes, measure how BTC state affects the
forward returns and cross-sectional dispersion of the tradable alt universe.

The goal is not to create trading signals directly. The goal is to produce
evidence for:

- BTC regime definitions.
- Whether BTC up/down states imply long, short, or no-trade bias for alts.
- Whether regime-specific feature selection is justified.
- Which symbols behave as high-beta amplifiers or defensive instruments.

## Hypotheses

1. BTC trend and drawdown state materially changes alt forward-return
   distributions.
2. The same alt feature can have different sign by BTC regime, so C-Auto should
   split or gate models by BTC state instead of training one mixed model.
3. BTC stress states should reduce trade frequency or flip the portfolio toward
   short/defensive selection.
4. Current-universe survivorship remains a risk, so all analysis must respect
   symbol listing coverage and point-in-time feature rows.

## Inputs

```text
feature dataset: c_auto_feature_store_v1
features: engine/data/features/c_auto_feature_store_v1/features.parquet
labels: engine/data/features/c_auto_feature_store_v1/labels.parquet
quality: engine/data/quality/c_auto_dataset_quality_v1/symbol_quality.parquet
primary label: fwd_ret_net_long_24
```

## BTC State Variables

Use BTC 1h close and point-in-time derived values:

- `btc_ret_1h`
- `btc_ret_4h`
- `btc_ret_24h`
- `btc_ret_7d`
- `btc_ret_30d`
- `btc_ema_20d`
- `btc_ema_60d`
- `btc_ema20_gt_ema60`
- `btc_drawdown_30d`
- `btc_rv_24h`
- `btc_rv_7d`

## First-Pass Regimes

Create six interpretable labels:

```text
deep_bear
bear
chop_short
chop_long
bull
strong_bull
```

Rules are intentionally simple for v1:

- `deep_bear`: BTC 30d return <= -20%, or 30d drawdown <= -25%, or below both
  EMAs with high 7d volatility and negative 7d return.
- `bear`: BTC below 20d and 60d EMAs with negative 7d or 30d return.
- `strong_bull`: BTC 30d return >= +20%, above 20d/60d EMAs, drawdown better
  than -8%.
- `bull`: BTC above 20d/60d EMAs with positive 7d or 30d return.
- `chop_long`: non-trend state with BTC above 20d EMA.
- `chop_short`: non-trend state with BTC below 20d EMA.

## Measurements

Per BTC regime:

- Row count and symbol count.
- Mean/median alt forward return for 1h, 4h, 12h, 24h.
- Positive hit rate.
- Cross-sectional dispersion: q80 - q20.
- Top-tail and bottom-tail forward returns.
- BTC return and volatility summary.

Per BTC return bucket:

- Bucket BTC 24h return into down/up quantiles.
- Measure alt 24h forward return distribution and dispersion.

Per symbol:

- Beta to BTC 1h returns.
- Correlation to BTC 1h returns.
- Mean alt 24h forward return by regime.
- Hit rate by regime.

## Acceptance Criteria

The study is useful if it produces:

- At least 3 regimes with materially different alt forward-return behavior.
- Clear no-trade or risk-off candidates.
- A defensible first regime definition for C-Auto v2.
- A short list of feature/model changes to test next.

## Output

```text
engine/data/research/c_auto/btc_alt_impact_v1/
  manifest.json
  regime_summary.csv
  regime_summary.parquet
  btc_return_bucket_summary.csv
  symbol_beta.csv
  symbol_regime_summary.csv
  btc_regime_timeline.parquet
```

## Follow-Up

If the study supports regime dependence, build:

```text
c_auto_feature_store_v2:
  + btc_regime_6
  + btc_regime_3
  + btc impact variables

c_auto_v2_regime_model:
  separate feature selection and model parameters by regime.
```

## Run Result

Completed: 2026-05-05T17:20:01+0800

```text
experiment_id: btc_alt_impact_v1
dataset_id: c_auto_feature_store_v1
output: engine/data/research/c_auto/btc_alt_impact_v1/
panel rows: 1,066,845
symbols: 78 alts + BTC state
btc state rows: 20,526
```

Regime results using `fwd_ret_net_long_24`:

| Regime | Rows | 24h alt mean | Hit rate | First bias |
|---|---:|---:|---:|---|
| `strong_bull` | 89,324 | +1.061% | 52.96% | long bias |
| `deep_bear` | 176,340 | +0.163% | 48.85% | selective/neutral |
| `chop_long` | 105,673 | -0.048% | 45.95% | selective/neutral |
| `bear` | 229,490 | -0.317% | 46.31% | short or avoid |
| `chop_short` | 109,173 | -0.421% | 45.86% | short or avoid |
| `bull` | 356,845 | -0.453% | 43.53% | short or avoid |

Important read:

- `strong_bull` is the only clearly positive broad long regime in this first
  pass.
- Ordinary `bull` is negative for broad alt longs after costs. Do not merge
  `bull` and `strong_bull` in C-Auto v2 without further testing.
- `deep_bear` has positive mean but weak hit rate. Treat as selective reversal
  research, not automatic long.
- `bear`, `chop_short`, and ordinary `bull` should start as short/avoid or
  stricter selection regimes.
- BTC 24h return buckets alone are not sufficient: the largest BTC-up bucket
  still shows negative broad alt 24h mean, while the rule-defined
  `strong_bull` regime is positive. Trend/drawdown context matters.

High-beta symbols in this universe:

```text
FARTCOIN, VIRTUAL, WIF, RENDER, ONDO, BIO, ORDI, TAO, INJ, WLD,
HYPE, SSV, AR, LDO, GRASS
```

Next action:

1. Add `btc_regime_6`, `btc_regime_3`, and BTC state columns to
   `c_auto_feature_store_v2`.
2. Run IC and feature selection by regime.
3. Build a first `c_auto_v2_regime_model` with separate selection logic for
   `strong_bull`, `risk_off`, and neutral states.
