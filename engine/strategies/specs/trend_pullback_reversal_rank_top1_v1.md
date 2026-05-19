# Trend Pullback Reversal Rank Top1 v1

Status: backtest candidate, not live enabled.

Shape:
- Same raw 4h trend / 1h pullback reversal candidate set as Quality Top20.
- Candidates are scored with the same fixed point-in-time quality score.
- Only the single highest-ranked candidate across the market is eligible each scan timestamp.

Selected path-simulated exits:
- Target: 5.0%
- Stop: 2.0%
- Max hold: 12h
- Same-bar target/stop collision: stop first

Backtest evidence:
- Source: `engine/data/research/trend_pullback_reversal/trend_pullback_tp_sl_quality20_rank1_20260517`
- Dataset: `c_auto_feature_store_rebuild_161_ohlcv_snapshot_v1`
- Period: 2025-01-01 to 2026-05-16
- Trades: 6029
- Win rate: 49.5605%
- Mean net return: 1.2664%
- Median net return: -0.4275%
- Total net return units: 76.3531
- Positive months: 17/17
- Worst month units: 0.8729

Data requirements:
- Current 1h OHLCV cache for entries/path simulation.
- 4h-derived features or 4h OHLCV cache for trend state.
- Feature dataset columns from `c_auto_feature_store_rebuild_161_ohlcv_snapshot_v1`.

Promotion notes:
- This is a concentrated capacity variant.
- Path simulation shows good monthly stability but weak trade-level median; keep behind Quality Top20 until paper evidence improves.
