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
