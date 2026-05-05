# C-Auto v2 Sleeve Experiment Plan

Created: 2026-05-05
Status: completed first screening + all-fold pass

## Objective

Test whether the four proposed C-Auto v2 money sources can become concrete
strategy sleeves:

1. BTC-regime alt cross-sectional spread
2. High-beta alt amplification
3. Crowding, squeeze, and reversal from OI/funding/long-short structure
4. Small-account fast rotation

## Inputs

Dataset: `c_auto_feature_store_v2`

Policy file:

```text
engine/strategies/specs/c_auto_v2_regime_policy.json
```

Runner:

```text
scripts/run_c_auto_v2_sleeve_experiments.py
```

Initial run:

```text
python3 scripts/run_c_auto_v2_sleeve_experiments.py --summary-id c_auto_v2_sleeve_experiments_v1
```

The first pass is a screening experiment using the policy default of 18
walk-forward folds. Any sleeve that passes screening should be rerun with all
54 folds and then promoted to delayed-execution portfolio backtests.

All-fold run:

```text
python3 scripts/run_c_auto_v2_sleeve_experiments.py --summary-id c_auto_v2_sleeve_experiments_allfold_v1 --max-folds 0
```

Summary artifact:

```text
engine/data/research/c_auto/c_auto_v2_sleeve_experiments_allfold_v1/summary.md
```

## All-Fold Results

| Sleeve | Regime | Label | IC | Spread | Long Tail | Short Tail | Rows | Folds |
|---|---|---|---:|---:|---:|---:|---:|---:|
| `cross_section_spread` | `strong_bull` | `fwd_ret_net_long_24` | +0.1570 | +0.0336 | +0.0224 | -0.0112 | 47,045 | 5 |
| `cross_section_spread` | `bear` | `fwd_ret_net_long_24` | +0.1975 | +0.0232 | +0.0095 | -0.0136 | 220,693 | 36 |
| `cross_section_spread` | `chop_short` | `fwd_ret_net_long_24` | +0.2419 | +0.0191 | +0.0041 | -0.0150 | 101,539 | 33 |
| `cross_section_spread` | `bull` | `fwd_ret_net_long_24` | +0.2758 | +0.0283 | +0.0107 | -0.0176 | 327,981 | 40 |
| `high_beta_amplification` | `strong_bull` | `fwd_ret_net_long_12` | +0.2707 | +0.0420 | +0.0261 | -0.0159 | 47,045 | 5 |
| `high_beta_amplification` | `bear` | `fwd_ret_net_short_12` | +0.3521 | +0.0299 | +0.0124 | -0.0175 | 220,693 | 36 |
| `small_account_rotation` | `strong_bull` | `fwd_ret_net_long_6` | +0.2318 | +0.0142 | +0.0077 | -0.0065 | 47,045 | 5 |
| `small_account_rotation` | `bear` | `fwd_ret_net_short_6` | +0.1740 | +0.0095 | +0.0030 | -0.0065 | 220,693 | 36 |
| `small_account_rotation` | `chop_short` | `fwd_ret_net_short_6` | +0.0842 | +0.0056 | +0.0041 | -0.0015 | 101,539 | 33 |
| `crowding_squeeze_reversal` | `strong_bull` | `fwd_ret_net_long_24` | +0.0014 | +0.0044 | +0.0069 | +0.0025 | 47,045 | 5 |
| `crowding_squeeze_reversal` | `bear` | `fwd_ret_net_short_24` | +0.0087 | -0.0008 | -0.0006 | +0.0003 | 220,693 | 36 |
| `crowding_squeeze_reversal` | `deep_bear` | `fwd_ret_net_long_12` | +0.0036 | -0.0010 | -0.0002 | +0.0008 | 165,908 | 22 |

## Interpretation

1. `cross_section_spread` is the core C-Auto v2 sleeve. It has positive IC
   and positive spread across tested regimes. `strong_bull` is long-tail
   friendly. `bear`, `chop_short`, and even ordinary `bull` are better treated
   as ranking environments than as simple broad market direction calls.

2. `high_beta_amplification` is the strongest conditional sleeve in this pass.
   It works as strong-bull long amplification and bear short amplification.
   This should be the first candidate for delayed-execution portfolio backtest
   together with the cross-section sleeve.

3. `small_account_rotation` has ranking signal, especially in strong bull and
   bear. Current combined-tail selection is not good enough; the next test
   should evaluate top-tail-only rotation with explicit max 3-6 active symbols.

4. `crowding_squeeze_reversal` should not be promoted as a standalone alpha
   sleeve. Its all-fold IC and spread are near zero or negative. Keep OI,
   funding, and long/short features as filters/risk controls for the other
   sleeves.

## Promotion Criteria

A sleeve is worth deeper testing when:

- Spearman IC is positive.
- Long-short spread is positive on the intended label.
- The result has enough folds and rows to avoid one-window dependence.
- The result matches the intended side policy rather than only looking good
  because the wrong tail made money.

No sleeve should be considered paper-tradable from this screening alone.

## Next Step

Build a delayed-execution portfolio backtest that combines:

- `cross_section_spread` as the base rank model,
- `high_beta_amplification` as a conditional booster,
- `crowding_squeeze_reversal` as a filter,
- `small_account_rotation` as a 6h top-tail portfolio construction variant.
