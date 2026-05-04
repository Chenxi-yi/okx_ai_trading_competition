# Feature Pipeline TODO

Last updated: 2026-04-24 13:54 CST

## Phase 1 — Research Data Backbone

- [x] Add feature registry metadata: feature name, family, source columns, lookback bars, frequency, live availability, NaN warmup expectation.
- [x] Add dataset manifest: dataset id, code version, input symbols, time range, row counts, feature registry snapshot, validation report, output files.
- [x] Add stronger validation: timestamp gaps, stale symbols, excessive NaN by feature, missing live-availability metadata.
- [x] Add cost-adjusted labels: forward returns net of fee, slippage, funding proxy, and minimum viable move.
- [x] Add purged walk-forward splitter for feature selection and model evaluation.

## Phase 2 — Alpha Data Expansion

- [x] Add Python collector for OHLCV, ticker, instrument metadata, funding, open interest, long/short ratio, trades, and orderbook snapshots.
- [x] Add versioned microstructure dataset manifest and artifact hashes.
- [x] Add first microstructure feature builder for orderbook, trade flow, open interest, funding, and long/short ratio artifacts.
- [x] Run BTC/ETH/SOL microstructure smoke dataset with all supported kinds.
- [x] Merge microstructure features into the main feature pipeline with point-in-time alignment.
- [ ] Order book features: top-of-book spread, depth imbalance, multi-level imbalance, liquidity slope.
- [ ] Trade flow features: aggressor buy/sell imbalance, trade count, trade size burst, volume acceleration.
- [ ] Derivatives features: open interest changes, funding regime, basis/funding stress.
- [ ] Cross-asset features: BTC/ETH regime, rolling beta, relative momentum, correlation crowding.
- [ ] Stress features: large wick events, volatility expansion, liquidation/proxy shock events.

## Phase 3 — Feature Selection To Strategy

- [ ] Cross-sectional IC by walk-forward fold.
- [ ] Time-series IC by symbol and regime.
- [ ] Feature stability score across symbols, folds, and regimes.
- [ ] Turnover/cost sensitivity score.
- [ ] Candidate signal generator that writes strategy/backtest configs from selected feature sets.

## Phase 4 — Backtest To Production

- [ ] Deterministic walk-forward backtests before Monte Carlo.
- [ ] Production feature snapshot logging using the same feature definitions.
- [ ] Decision journal links: dataset version, feature version, signal version, risk state, order ids.
- [ ] Environment-specific runtime summaries for `demo`, `live`, and `personal`.
- [ ] Monitoring for feature drift, signal drift, data gaps, slippage, fill quality, and kill-switch state.
