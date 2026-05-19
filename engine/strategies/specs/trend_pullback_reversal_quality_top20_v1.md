# Trend Pullback Reversal Quality Top20 v1

Status: backtest candidate, not live enabled.

Shape:
- 4h trend establishes direction.
- 1h countertrend pullback must stay within the configured pullback limit.
- 1h reversal bar must confirm back toward the 4h trend.
- Entry candidates are scored with fixed point-in-time quality components.
- Only the top 20% by quality score per scan timestamp are eligible.

Selected path-simulated exits:
- Target: 5.0%
- Stop: 1.5%
- Max hold: 12h
- Same-bar target/stop collision: stop first

Backtest evidence:
- Source: `engine/data/research/trend_pullback_reversal/trend_pullback_tp_sl_quality20_rank1_20260517`
- Dataset: `c_auto_feature_store_rebuild_161_ohlcv_snapshot_v1`
- Period: 2025-01-01 to 2026-05-16
- Trades: 8413
- Win rate: 50.0773%
- Mean net return: 1.5580%
- Median net return: 0.0732%
- Total net return units: 131.0770
- Positive months: 17/17
- Worst month units: 1.3799

Data requirements:
- Current 1h OHLCV cache for entries/path simulation.
- 4h-derived features or 4h OHLCV cache for trend state.
- Feature dataset columns from `c_auto_feature_store_rebuild_161_ohlcv_snapshot_v1`.

Promotion notes:
- Candidate may enter paper/competition readiness after a live feature parity check.
- Do not enable live allocation until paper runner emits forward decision journals.
