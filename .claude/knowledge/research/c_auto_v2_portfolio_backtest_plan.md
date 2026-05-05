# C-Auto v2 Portfolio Backtest Plan

Created: 2026-05-05
Status: MTM and fold-leakage checks completed

## Objective

Move from sleeve-level ranking diagnostics to a delayed-execution portfolio
test for C-Auto v2.

The portfolio combines:

- `cross_section_spread` as the base rank model,
- `high_beta_amplification` as the conditional booster,
- `crowding_squeeze_reversal` as a filter rather than a standalone alpha,
- `small_account_rotation` as a 6h/12h fast selection component.

## Backtest Rules

- Signals are calculated at timestamp `t`.
- Entries execute at `t + 1h`.
- Positions exit after the selected horizon:
  - `strong_bull`: long, 12h
  - `bear`: short, 12h
  - `chop_short`: short, 6h
  - `bull`: selective long, 24h with reduced risk
- Maximum active positions: 5
- Rebalance interval: 6h
- Initial capital: 1,000 USDT
- Per-side fee: 5 bps
- Per-side slippage: 2 bps
- Round-trip cost: 14 bps
- Crowding filter blocks extreme funding/OI/long-short conditions.

## Runner

```text
python3 scripts/backtest_c_auto_v2_portfolio.py --out-id c_auto_v2_portfolio_backtest_v1
```

Artifacts are written under:

```text
engine/data/research/c_auto/c_auto_v2_portfolio_backtest_v1/
```

## First Results

Baseline run:

```text
python3 scripts/backtest_c_auto_v2_portfolio.py --out-id c_auto_v2_portfolio_backtest_v1
```

Result:

| Metric | Value |
|---|---:|
| Window | 2024-04-01 to 2026-04-26 |
| Initial NAV | 1,000.00 |
| Final NAV | 383,989.70 |
| Total return | +38,298.97% |
| Max drawdown | -14.89% |
| Trades | 3,265 |
| Win rate | 57.43% |
| Avg net return/trade | +1.5315% |
| Total costs | 32,424.77 |

The baseline run is directionally useful but too aggressive to trust as a
deployment profile because compounding 18% NAV per position creates very large
notional turnover.

Conservative run:

```text
python3 scripts/backtest_c_auto_v2_portfolio.py \
  --out-id c_auto_v2_portfolio_backtest_conservative_v1 \
  --base-risk 0.06 \
  --max-positions 4 \
  --min-score-quantile 0.9
```

Result:

| Metric | Value |
|---|---:|
| Initial NAV | 1,000.00 |
| Final NAV | 6,227.26 |
| Total return | +522.73% |
| Max drawdown | -4.67% |
| Trades | 2,612 |
| Win rate | 57.43% |
| Avg net return/trade | +1.7126% |
| Total costs | 442.44 |

High-cost conservative run:

```text
python3 scripts/backtest_c_auto_v2_portfolio.py \
  --out-id c_auto_v2_portfolio_backtest_high_cost_v1 \
  --fee-bps-per-side 8 \
  --slippage-bps-per-side 8 \
  --base-risk 0.06 \
  --max-positions 4 \
  --min-score-quantile 0.9
```

Result:

| Metric | Value |
|---|---:|
| Final NAV | 5,069.66 |
| Total return | +406.97% |
| Max drawdown | -4.99% |
| Trades | 2,612 |
| Win rate | 54.44% |
| Avg net return/trade | +1.5326% |
| Total costs | 888.10 |

Year slices with baseline risk:

| Slice | Final NAV | Total Return | Max DD | Trades | Win Rate |
|---|---:|---:|---:|---:|---:|
| 2024-04-01 to 2024-12-31 | 12,804.87 | +1,180.49% | -14.89% | 1,375 | 55.71% |
| 2025-01-01 to 2025-12-31 | 11,263.14 | +1,026.31% | -8.78% | 1,530 | 57.78% |
| 2026-01-01 to 2026-04-26 | 2,213.82 | +121.38% | -14.43% | 330 | 60.61% |

Conservative 14-day windows:

Worst observed 14-day window:

```text
2026-02-02 to 2026-02-16: -3.20%
```

Best observed 14-day window:

```text
2024-11-11 to 2024-11-25: +19.50%
```

## Interpretation

This pass supports the combined architecture:

- cross-section + high-beta is the profitable core,
- shorts in `bear` and `chop_short` are meaningful, not just hedges,
- ordinary `bull` should remain selective rather than broad long,
- crowding/funding/OI filters did not destroy the edge under higher costs.

The current result is still not paper-trade ready. The backtest uses delayed
entry and explicit round-trip costs, but equity is marked mainly on realized
position exits. The next validation step should add mark-to-market open PnL,
fixed-notional or volatility-targeted sizing, and stricter leakage checks
around prediction fold boundaries.

## MTM And Fold-Leakage Checks

The backtest runner now records:

- `nav_mtm`: realized NAV plus marked-to-market open PnL,
- `realized_nav`: closed-trade NAV only,
- `unrealized_pnl`: current open PnL,
- `fold_ids`: prediction fold references used by each trade.

Metrics now use `nav_mtm` for total return and drawdown. Each trade is checked
against `walk_forward_folds.json`; every prediction fold used by the trade must
place `signal_ts` inside that fold's test window.

MTM baseline:

```text
python3 scripts/backtest_c_auto_v2_portfolio.py --out-id c_auto_v2_portfolio_backtest_mtm_v1
```

| Metric | Previous Realized-Only | MTM |
|---|---:|---:|
| Final NAV | 383,989.70 | 380,367.55 |
| Total return | +38,298.97% | +37,936.75% |
| Max drawdown | -14.89% | -19.21% |
| Trades | 3,265 | 3,265 |
| Leakage violations | n/a | 0 |

MTM conservative:

```text
python3 scripts/backtest_c_auto_v2_portfolio.py \
  --out-id c_auto_v2_portfolio_backtest_mtm_conservative_v1 \
  --base-risk 0.06 \
  --max-positions 4 \
  --min-score-quantile 0.9
```

| Metric | Previous Realized-Only | MTM |
|---|---:|---:|
| Final NAV | 6,227.26 | 6,222.14 |
| Total return | +522.73% | +522.21% |
| Max drawdown | -4.67% | -6.46% |
| Trades | 2,612 | 2,612 |
| Leakage checked fold refs | n/a | 6,564 |
| Leakage violations | n/a | 0 |
| Missing fold trades | n/a | 0 |

MTM high-cost conservative:

```text
python3 scripts/backtest_c_auto_v2_portfolio.py \
  --out-id c_auto_v2_portfolio_backtest_mtm_high_cost_v1 \
  --fee-bps-per-side 8 \
  --slippage-bps-per-side 8 \
  --base-risk 0.06 \
  --max-positions 4 \
  --min-score-quantile 0.9
```

| Metric | MTM |
|---|---:|
| Final NAV | 5,065.34 |
| Total return | +406.53% |
| Max drawdown | -6.71% |
| Trades | 2,612 |
| Win rate | 54.44% |
| Leakage violations | 0 |

Conservative MTM 14-day windows:

Worst observed 14-day window:

```text
2026-02-02 to 2026-02-16: -1.63%
```

Best observed 14-day window:

```text
2024-11-11 to 2024-11-25: +19.63%
```

## Updated Interpretation

The two checks did not break the edge:

- MTM made drawdown meaningfully worse, as expected, but did not remove the
  conservative or high-cost conservative profitability.
- Fold-leakage checking passed: 6,564 fold references checked, 0 violations,
  0 missing fold trades in the conservative run.

Remaining blockers before paper trading:

- Add fixed-notional or volatility-targeted sizing to reduce compounding
  sensitivity.
- Add a stricter symbol listing-age mask for newly listed instruments.
- Run a paper-mode dry deployment that emits signals without placing orders.
