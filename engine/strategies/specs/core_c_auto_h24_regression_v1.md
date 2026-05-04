# core_c_auto_h24_regression_v1

## Identity

- Book: core
- Timeframe: 1h
- Holding period: 12h-36h
- Runtime role: strategy-only signal generator
- Live default: disabled

## Hypothesis

Crypto perpetuals have short-lived 24h cross-sectional return structure that can
be learned from point-in-time trend, volatility, range, volume, and funding
features. The strategy should only trade when the predicted edge is large enough
to clear fees, slippage, and adverse path risk.

## Required Data

- 1h OHLCV for each symbol in the liquid OKX USDT swap universe
- Funding rate when available
- Instrument metadata supplied by the execution/risk layer

## Signal Logic

1. Build a point-in-time feature panel with only backward-looking features.
2. Build H24 forward-return labels for historical rows.
3. Train a rolling Ridge regression model on completed labels only.
4. Score the latest cross-section.
5. Emit long signals from the upper prediction tail and short signals from the
   lower prediction tail.
6. Do not size orders directly. The portfolio arbiter, account risk arbiter, and
   execution router own sizing, contract conversion, and order placement.

## Default Parameters

- `label_horizon_bars`: 24
- `train_window_bars`: 2520
- `min_train_rows`: 400
- `max_signals`: 6
- `long_quantile`: 0.80
- `short_quantile`: 0.20
- `min_abs_prediction`: 0.0015
- `target_pct`: 0.03
- `stop_pct`: 0.015
- `horizon_sec`: 86400
- `ridge_alpha`: 10.0

## Promotion Gates

- Research: feature/label validation has no lookahead or missing-index defects.
- Backtest: at least 12 months, plus 14-day chunk review.
- Paper: at least 14 live-market days with positive expectancy after costs.
- Live: manual approval only, allocation starts at 0 and must remain inside the
  core-book risk budget.

## Known Failure Modes

- Regime breaks can turn learned trend/funding effects into noise.
- Newly listed symbols can pass ranks with too little history.
- 1h OHLCV does not capture liquidation cascades or spread blowouts.
- Cross-sectional tails can become crowded during market-wide deleveraging.
