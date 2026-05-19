# Trend Pullback Reversal Cluster Elite Quality60 v1

Status: backtest candidate, not live enabled.

Shape:
- 4h trend establishes direction.
- 1h countertrend pullback must stay within the configured pullback limit.
- 1h reversal bar must confirm back toward the 4h trend.
- Rolling cluster gate keeps only elite historical clusters:
  - cluster k: 6
  - training window: 180d
  - refit cadence: 24h
  - min prior count: 80
  - min prior win rate: 60%
  - min prior mean net return: 1.00%
- Second-stage quality filter: `quality_score >= 0.60`.

Selected path-simulated exits:
- Target: 5.0%
- Stop: 0.8%
- Max hold: 12h
- Same-bar target/stop collision: stop first

Backtest evidence:
- Source: `engine/data/research/trend_pullback_reversal/trend_pullback_tp_sl_three_candidates_20260517`
- Dataset: `c_auto_feature_store_rebuild_161_ohlcv_snapshot_v1`
- Period: 2025-01-01 to 2026-05-16
- Trades: 1142
- Win rate: 55.5166%
- Mean net return: 2.2732%
- Median net return: 4.8600%
- Total net return units: 25.9596
- Positive months: 17/17
- Worst month units: 0.1286

Data requirements:
- Current 1h OHLCV cache for entries/path simulation.
- Current 4h OHLCV cache or 4h-derived trend features.
- Funding, open-interest, and long/short snapshots where available.
- BTC regime/state features derived from refreshed OHLCV.

Promotion notes:
- This is the highest-quality current candidate by trade-level median and win rate.
- Capacity is smaller than Quality Top20; treat as high-conviction sleeve.
- Do not enable live allocation until paper runner confirms rolling-cluster parity.
