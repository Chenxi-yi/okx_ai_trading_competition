# C-Auto v2 — BTC-Regime Alt Cross-Section

Created: 2026-05-05
Status: research / strategy thesis
Primary strategy family: `core_c_auto_h24_regression_v1` successor
Target capital scale: 1,000 USDT

## Core View

At 1,000 USDT scale, C-Auto should not try to make most of its money by
predicting BTC direction directly. BTC moves are too small relative to fees,
slippage, and opportunity cost unless leverage is pushed too hard.

The realistic edge is:

```text
BTC regime defines the market environment
  -> alts provide amplified and uneven reactions
  -> model ranks which alts should be long/short
  -> OI/funding/volume/liquidity filters avoid crowded bad trades
  -> small account executes quickly with low market impact
```

C-Auto v2 should therefore earn from BTC-regime-conditioned alt
cross-sectional spread, not from broad market beta.

## What Money We Are Trying To Earn

1. BTC-regime alt cross-sectional spread

Same BTC environment, different alt behavior. The model should rank:

- which alts are most likely to outperform,
- which alts are most likely to underperform,
- when dispersion is too weak to trade.

2. High-beta alt amplification

BTC supplies the directional/environment trigger. Selected alts supply
amplitude. A small BTC move can become a much larger alt move, but only in the
right regime and with the right participation.

3. Crowding and squeeze/reversal money

Funding, OI, and long/short data are not just add-on features. They help
separate:

- trend continuation with healthy participation,
- crowded late longs,
- failed bounces in bear markets,
- post-liquidation rebound windows.

4. Small-account execution flexibility

With 1,000 USDT, the account can rotate quickly across 3-6 signals without
meaningful market impact. The intended holding window is roughly 4h-24h, not
multi-week trend following.

5. No-trade edge

Avoiding weak regimes matters. A 1,000 USDT account can be damaged quickly by
fees and churn. C-Auto v2 should explicitly choose no-trade or reduced-risk
states.

## Current Evidence

Feature store:

```text
dataset_id: c_auto_feature_store_v2
rows: 1,087,371
features: 88
labels: 40
btc state/regime features: 22
```

BTC impact study:

```text
experiment_id: btc_alt_impact_v1
panel rows: 1,066,845
alts: 78
```

Key regime results for 24h net long labels:

| BTC regime | Alt 24h mean | Hit rate | First bias |
|---|---:|---:|---|
| `strong_bull` | +1.061% | 52.96% | long bias |
| `deep_bear` | +0.163% | 48.85% | selective / reversal research |
| `chop_long` | -0.048% | 45.95% | selective / neutral |
| `bear` | -0.317% | 46.31% | short / two-sided ranking |
| `chop_short` | -0.421% | 45.86% | short / avoid |
| `bull` | -0.453% | 43.53% | avoid broad long |

Important implication:

- Do not merge `bull` and `strong_bull`.
- Ordinary BTC uptrend is not automatically good for broad alt longs.
- Strong bull is the clearest long-bias regime.
- Bear regime has useful ranking spread and should be modeled as two-sided or
  short-tail-priority, not simply "short everything".

## Current Smoke Results

Default mixed v1 baseline:

```text
dataset: c_auto_feature_store_v1
experiment: c_auto_feature_store_v1_baseline_12fold
spearman_ic: -0.0428
long_short_spread: -0.00130
verdict: not promotable
```

Regime-aware v2 smoke:

```text
experiment: c_auto_v2_strong_bull_top20_allfold
regime: strong_bull
rows: 47,045
folds with samples: 5
spearman_ic: +0.1570
long_short_spread: +0.03359
long_tail_mean_return: +0.02242
short_tail_mean_return: -0.01117
```

```text
experiment: c_auto_v2_bear_top20_allfold
regime: bear
rows: 220,693
folds with samples: 36
spearman_ic: +0.1975
long_short_spread: +0.02315
long_tail_mean_return: +0.00954
short_tail_mean_return: -0.01361
```

These are smoke tests using `fallback_linear_score`, because local sklearn is
not installed. They are evidence for the architecture, not final model claims.

## Sleeve Experiment Results

Experiment plan:
`.claude/knowledge/research/c_auto_v2_sleeve_experiment_plan.md`

Policy:
`engine/strategies/specs/c_auto_v2_regime_policy.json`

All-fold summary:
`engine/data/research/c_auto/c_auto_v2_sleeve_experiments_allfold_v1/summary.md`

Key all-fold results:

| Sleeve | Regime | Label | IC | Spread | Long Tail | Short Tail |
|---|---|---|---:|---:|---:|---:|
| `cross_section_spread` | `strong_bull` | 24h long | +0.1570 | +0.0336 | +0.0224 | -0.0112 |
| `cross_section_spread` | `bear` | 24h long | +0.1975 | +0.0232 | +0.0095 | -0.0136 |
| `cross_section_spread` | `chop_short` | 24h long | +0.2419 | +0.0191 | +0.0041 | -0.0150 |
| `cross_section_spread` | `bull` | 24h long | +0.2758 | +0.0283 | +0.0107 | -0.0176 |
| `high_beta_amplification` | `strong_bull` | 12h long | +0.2707 | +0.0420 | +0.0261 | -0.0159 |
| `high_beta_amplification` | `bear` | 12h short | +0.3521 | +0.0299 | +0.0124 | -0.0175 |
| `small_account_rotation` | `strong_bull` | 6h long | +0.2318 | +0.0142 | +0.0077 | -0.0065 |
| `small_account_rotation` | `bear` | 6h short | +0.1740 | +0.0095 | +0.0030 | -0.0065 |

Interpretation:

- `cross_section_spread` remains the primary sleeve.
- `high_beta_amplification` is the strongest conditional booster and should be
  included in the first portfolio backtest.
- `small_account_rotation` has signal but needs top-tail-only construction;
  the current combined-tail selected return is not enough.
- `crowding_squeeze_reversal` is not a standalone alpha after all-fold testing.
  Keep funding, OI, and long/short data as filters and risk controls.

## Portfolio Backtest Results

Portfolio plan:
`.claude/knowledge/research/c_auto_v2_portfolio_backtest_plan.md`

Runner:
`scripts/backtest_c_auto_v2_portfolio.py`

First delayed-execution pass:

```text
signals at t -> entry at t+1h
max positions: 5 baseline / 4 conservative
rebalance: 6h
round-trip cost baseline: 14 bps
initial capital: 1,000 USDT
```

Key runs:

| Run | Final NAV | Return | Max DD | Trades | Win Rate |
|---|---:|---:|---:|---:|---:|
| `c_auto_v2_portfolio_backtest_v1` | 383,989.70 | +38,298.97% | -14.89% | 3,265 | 57.43% |
| `c_auto_v2_portfolio_backtest_conservative_v1` | 6,227.26 | +522.73% | -4.67% | 2,612 | 57.43% |
| `c_auto_v2_portfolio_backtest_high_cost_v1` | 5,069.66 | +406.97% | -4.99% | 2,612 | 54.44% |

The conservative profile is the more relevant research reference. It uses 6%
base risk per position, 4 max positions, and only the top 10% candidates.

Important caveat:

The first portfolio backtest has delayed entry and explicit costs, but it is
still a research simulator. Before paper trading, add mark-to-market open PnL,
fixed-notional or volatility-targeted sizing, and stricter fold-boundary
leakage checks.

MTM and fold-leakage follow-up:

| Run | Final NAV | Return | Max DD | Leakage |
|---|---:|---:|---:|---|
| `c_auto_v2_portfolio_backtest_mtm_v1` | 380,367.55 | +37,936.75% | -19.21% | 0 violations |
| `c_auto_v2_portfolio_backtest_mtm_conservative_v1` | 6,222.14 | +522.21% | -6.46% | 0 violations |
| `c_auto_v2_portfolio_backtest_mtm_high_cost_v1` | 5,065.34 | +406.53% | -6.71% | 0 violations |

The MTM check increased drawdown but did not remove the edge. The fold-leakage
check passed for the conservative run with 6,564 checked fold references, 0
violations, and 0 missing fold trades.

Fixed 1,000U sizing:

| Run | Final NAV | Return | Max DD | Leakage |
|---|---:|---:|---:|---|
| `c_auto_v2_portfolio_backtest_fixed1000_v1` | 7,246.86 | +624.69% | -8.86% | 0 violations |
| `c_auto_v2_portfolio_backtest_fixed1000_conservative_v1` | 2,854.45 | +185.44% | -3.05% | 0 violations |
| `c_auto_v2_portfolio_backtest_fixed1000_high_cost_v1` | 2,648.21 | +164.82% | -3.36% | 0 violations |

Fixed sizing is the current reference view because it removes compounding from
position sizing. The conservative fixed profile is the best paper-mode
candidate so far.

## Rebuild-161 OHLCV-Only Check — 2026-05-08

After rebuilding OKX OHLCV history from `2023-01-01` to `2026-05-07`, the
training universe was filtered to remove symbols first available on or after
`2026-03-08`.

Inputs:

```text
universe: okx_usdt_swap_ge2m_20260507_listed_before_20260308
symbols: 161
quality_id: c_auto_dataset_quality_rebuild_161_ohlcv_v1
feature_store: c_auto_feature_store_rebuild_161_ohlcv_v1
feature rows: 2,572,026
features: 83
labels: 40
walk-forward folds: 80
validation: warn:excessive_feature_nan
```

The warning is expected for this pass because derivatives and instrument
snapshot data were not rebuilt yet. OHLCV, BTC regime, and price-derived
features were available; `oi_*`, `ls_*`, and `listing_age_days` were all-null.

All-fold sleeve check on the rebuilt feature store:

| Sleeve | Regime | Label | IC | Spread | Long Tail | Short Tail |
|---|---|---:|---:|---:|---:|---:|
| `cross_section_spread` | `strong_bull` | 24h long | +0.2110 | +0.0398 | +0.0327 | -0.0070 |
| `cross_section_spread` | `bear` | 24h long | +0.2094 | +0.0235 | +0.0099 | -0.0136 |
| `cross_section_spread` | `chop_short` | 24h long | +0.2241 | +0.0188 | +0.0059 | -0.0130 |
| `cross_section_spread` | `bull` | 24h long | +0.2966 | +0.0294 | +0.0134 | -0.0160 |
| `high_beta_amplification` | `strong_bull` | 12h long | +0.3056 | +0.0404 | +0.0276 | -0.0129 |
| `high_beta_amplification` | `bear` | 12h short | +0.3202 | +0.0286 | +0.0122 | -0.0163 |
| `small_account_rotation` | `strong_bull` | 6h long | +0.2034 | +0.0146 | +0.0096 | -0.0050 |
| `small_account_rotation` | `bear` | 6h short | +0.1690 | +0.0092 | +0.0031 | -0.0061 |

Fixed 1,000U conservative portfolio checks:

| Run | Final NAV | Return | Max DD | Trades | Win Rate | Leakage |
|---|---:|---:|---:|---:|---:|---|
| `c_auto_v2_portfolio_rebuild_161_ohlcv_fixed1000_conservative_v1` | 6,120.74 | +512.07% | -3.87% | 4,200 | 60.69% | 0 violations |
| `c_auto_v2_portfolio_rebuild_161_ohlcv_fixed1000_highcost_v1` | 5,819.95 | +482.00% | -4.25% | 4,200 | 58.45% | 0 violations |
| `c_auto_v2_portfolio_rebuild_161_ohlcv_fixed1000_2025plus_v1` | 2,936.44 | +193.64% | -4.67% | 1,504 | 62.37% | 0 violations |
| `c_auto_v2_portfolio_rebuild_161_ohlcv_fixed1000_2026_v1` | 1,532.10 | +53.21% | -8.58% | 264 | 68.18% | 0 violations |

Interpretation:

- The rebuilt OHLCV-only pass reproduces and strengthens the C-Auto v2 thesis.
- `cross_section_spread` and `high_beta_amplification` remain the first paper
  candidates.
- `crowding_squeeze_reversal` is not validated without derivatives and should
  remain disabled until OI/long-short history is rebuilt.
- Before live allocation, rebuild derivatives/instrument snapshots and rerun the
  same sleeve and portfolio tests with derivative features present.

## Rebuild-161 Snapshot Check — 2026-05-08

Market snapshot run:

```text
run_id: rebuild_161_market_snapshot_20260508
kinds: instrument,ticker,orderbook,trades
symbols: 161
jobs: 644
status: completed
failed: 0
```

Feature store with snapshots:

```text
dataset_id: c_auto_feature_store_rebuild_161_ohlcv_snapshot_v1
rows: 2,572,026
features: 88
labels: 40
validation: warn:excessive_feature_nan
```

The snapshot pass successfully adds static/instrument features. In particular,
`listing_age_days` is now populated and appears in the top IC diagnostics. The
remaining excessive-NaN warning is expected because historical funding,
open-interest, and long/short data have not been rebuilt yet.

All-fold sleeve check with snapshots:

| Sleeve | Regime | Label | IC | Spread | Long Tail | Short Tail |
|---|---|---:|---:|---:|---:|---:|
| `cross_section_spread` | `strong_bull` | 24h long | +0.2064 | +0.0384 | +0.0324 | -0.0060 |
| `cross_section_spread` | `bear` | 24h long | +0.2083 | +0.0227 | +0.0094 | -0.0133 |
| `cross_section_spread` | `chop_short` | 24h long | +0.2210 | +0.0186 | +0.0057 | -0.0129 |
| `cross_section_spread` | `bull` | 24h long | +0.2988 | +0.0293 | +0.0132 | -0.0160 |
| `high_beta_amplification` | `strong_bull` | 12h long | +0.3056 | +0.0404 | +0.0276 | -0.0129 |
| `high_beta_amplification` | `bear` | 12h short | +0.3202 | +0.0286 | +0.0122 | -0.0163 |
| `small_account_rotation` | `strong_bull` | 6h long | +0.2018 | +0.0145 | +0.0095 | -0.0050 |
| `small_account_rotation` | `bear` | 6h short | +0.1677 | +0.0090 | +0.0030 | -0.0061 |

Fixed 1,000U conservative portfolio checks with snapshots:

| Run | Final NAV | Return | Max DD | Trades | Win Rate | Leakage |
|---|---:|---:|---:|---:|---:|---|
| `c_auto_v2_portfolio_rebuild_161_ohlcv_snapshot_fixed1000_conservative_v1` | 6,107.42 | +510.74% | -3.49% | 4,200 | 60.60% | 0 violations |
| `c_auto_v2_portfolio_rebuild_161_ohlcv_snapshot_fixed1000_2025plus_v1` | 2,916.93 | +191.69% | -4.44% | 1,504 | 61.84% | 0 violations |
| `c_auto_v2_portfolio_rebuild_161_ohlcv_snapshot_fixed1000_2026_v1` | 1,539.76 | +53.98% | -7.62% | 264 | 68.94% | 0 violations |

Interpretation:

- Snapshot features do not materially change the portfolio result versus the
  OHLCV-only pass, which is good: the core signal is stable.
- `listing_age_days` is useful for diagnostics and gating, but not the main
  alpha source.
- The next data priority is historical funding, open-interest, and long/short
  history. Only after that should `crowding_squeeze_reversal` be judged again.

## Rebuild-161 Funding/OI Check — 2026-05-08

Historical derivatives run:

```text
run_id: rebuild_161_funding_oi_1d_20230101_20260507
kinds: funding,open_interest
timeframe: 1d
symbols: 161
jobs: 322
status: completed
failed: 0
```

Important data caveat:

- Funding history has useful multi-month to multi-year coverage.
- OKX daily open-interest history returned mostly 180 rows per symbol, with
  some 100-row symbols. Treat this as recent OI structure, not complete 2023
  history.
- `long_short` does not accept the `1d` period on OKX and failed with
  `Parameter period error`; it must be rebuilt separately with a supported
  period such as `1h` or a shorter 5m window.

Feature store:

```text
dataset_id: c_auto_feature_store_rebuild_161_funding_oi_1d_snapshot_v1
rows: 2,572,026
features: 88
labels: 40
validation: warn:excessive_feature_nan
```

All-fold sleeve check with funding/OI 1d plus snapshots:

| Sleeve | Regime | Label | IC | Spread | Long Tail | Short Tail |
|---|---|---:|---:|---:|---:|---:|
| `cross_section_spread` | `strong_bull` | 24h long | +0.2064 | +0.0384 | +0.0324 | -0.0060 |
| `cross_section_spread` | `bear` | 24h long | +0.2083 | +0.0227 | +0.0094 | -0.0133 |
| `cross_section_spread` | `chop_short` | 24h long | +0.2210 | +0.0186 | +0.0057 | -0.0129 |
| `cross_section_spread` | `bull` | 24h long | +0.2988 | +0.0293 | +0.0132 | -0.0160 |
| `high_beta_amplification` | `strong_bull` | 12h long | +0.3056 | +0.0404 | +0.0276 | -0.0129 |
| `high_beta_amplification` | `bear` | 12h short | +0.3202 | +0.0286 | +0.0122 | -0.0163 |
| `crowding_squeeze_reversal` | `strong_bull` | 24h long | +0.0142 | -0.0002 | +0.0088 | +0.0090 |
| `crowding_squeeze_reversal` | `bear` | 24h short | +0.0072 | -0.0014 | -0.0006 | +0.0008 |
| `crowding_squeeze_reversal` | `deep_bear` | 12h long | +0.0008 | -0.0005 | +0.0002 | +0.0007 |

Fixed 1,000U conservative portfolio check:

| Run | Final NAV | Return | Max DD | Trades | Win Rate | Leakage |
|---|---:|---:|---:|---:|---:|---|
| `c_auto_v2_portfolio_rebuild_161_funding_oi_1d_snapshot_fixed1000_conservative_v1` | 6,072.91 | +507.29% | -3.49% | 4,200 | 60.52% | 0 violations |

Interpretation:

- Funding/OI 1d does not add meaningful standalone alpha in this pass.
- The core C-Auto edge remains price/regime cross-sectional ranking plus
  high-beta amplification.
- `crowding_squeeze_reversal` should stay disabled as a standalone sleeve.
- Use funding/OI first as risk filters, and only re-test crowding after
  supported-period `long_short` data is available.

## Proposed C-Auto v2 Architecture

```text
1. BTC Regime Engine
   input: BTC 1h returns, 4h/1d trend, 30d drawdown, realized volatility
   output: btc_regime_6 and btc_regime_3

2. Regime Policy
   strong_bull: long-biased top-tail selection
   bear: two-sided or short-tail-priority selection
   deep_bear: selective reversal or risk-off until proven
   chop_long: selective only
   chop_short: short/avoid
   bull: avoid broad long; require strong cross-sectional confirmation

3. Regime-Specific Rank Models
   train separate feature sets or parameters per BTC regime.
   primary label: fwd_ret_net_long_24.
   also evaluate short-tail labels and long-short spread.

4. Market Structure Filters
   OI change, funding, long/short ratio, volume/range expansion, listing age,
   and liquidity gates.

5. Portfolio Construction
   3-6 active symbols.
   small per-symbol risk.
   reduce or disable trading when regime spread is weak.

6. Execution And Risk
   strategy emits signals only.
   execution remains through existing broker/Agent Trade Kit path.
   paper first; no live enable by default.
```

## Application To Existing C-Auto

Keep `core_c_auto_h24_regression_v1` as the old baseline implementation. Build
v2 incrementally:

1. Add a parameter set using regime-specific feature columns.
2. Modify strategy generation so current BTC regime chooses the active feature
   set and long/short policy.
3. Add a no-trade gate when the current regime has no validated policy.
4. Add liquidity/crowding filters after rank scoring.
5. Run delayed-execution backtests before paper trading.

The first implementation should not add new live order paths. It should still
emit `Signal` objects only.

## Next Experiments

1. Build `c_auto_v2_regime_policy.json`
   - map each regime to allowed side, feature set, quantiles, and risk scalar.

2. Run full regime smoke experiments:
   - `strong_bull`
   - `bear`
   - `chop_short`
   - `bull`
   - `deep_bear`
   - `chop_long`

3. Backtest v2 with delayed execution:
   - next-bar execution,
   - taker fees,
   - spread/slippage floor,
   - funding adjustment,
   - max 3-6 active positions.

4. Install or vendor a proper ML backend:
   - sklearn Ridge first,
   - then LightGBM/CatBoost if available.

## Promotion Gates

Do not paper trade until:

- Regime-specific backtest beats mixed baseline after costs.
- Results survive 14-day chunks.
- Strong bull is not dependent on one short calendar window.
- Bear regime behavior is validated for both long-tail and short-tail use.
- Liquidity and funding/OI filters reduce tail losses rather than overfit.
