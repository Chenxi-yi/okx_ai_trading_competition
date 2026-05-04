# Monster Coin Catcher

## Purpose

Research and trade high-convexity altcoin breakouts: coins that can move 40% intraday or several-fold over multi-day windows. This strategy is currently research/paper-first. It must not place live orders until chronological backtests, walk-forward checks, and paper trading are acceptable.

## Current Data

- 5m OHLCV cache for 132 OKX swap symbols: `engine/data/training_history/train_hist_134_5m_20240101_20260424/manifest.json`
- Clustered event labels: `engine/data/monster_events/monster_episodes_5m_v1/episodes.parquet`
- Clustered training samples: `engine/data/monster_events/monster_samples_clustered_5m_v1/samples.parquet`
- Live-gated watchlist: `engine/data/monster_events/monster_watchlist_5m_live_gated_20260426/`
- Derivatives structure data exists, but historical OI/long-short coverage is too short for the first training pass.

## Signal

The score is a percentile ensemble over point-in-time features selected by AUC distance from clustered monster labels. It currently emphasizes:

- Volatility expansion: `rvol_1h`, `rvol_3h`, `rvol_6h`, `rvol_12h`, `rvol_24h`
- Range expansion: `range_pct_15m`, `range_pct_1h`, `range_pct_3h`, `range_pct_6h`, `range_pct_12h`, `range_pct_24h`
- Distance from recent high: low `dist_high_*` means the coin is near a local high
- Cross-sectional strength: `cs_rank_ret_6h`, `cs_rank_ret_24h`
- Market regime filter: suppress broad-market pump/dump periods via `market_event_flag`

## Live Candidate Gates

- Fresh cached bar age <= 15 minutes
- OKX ticker quote volume >= 1,000,000 USDT
- Spread <= 50 bps
- 1% orderbook depth >= 5,000 USDT
- Adjusted monster score >= 0.75
- 1h return <= 25% to avoid chasing fully extended candles

## Backtest V1

Command:

```bash
python3 scripts/backtest_monster_strategy.py --dataset-id monster_backtest_5m_v1 --start 2025-01-01 --end 2026-04-26 --rebalance-minutes 240
```

Assumptions:

- Long-only notional allocation.
- Signal at decision bar close, entry on next 5m open.
- 4 bps fee and 4 bps slippage per side.
- Conservative exit priority: stop/trailing stop is hit before take-profit if both occur inside one bar.
- No funding costs in V1.
- No leverage in V1; this validates signal quality before aggressive sizing.

Default risk:

- Initial capital: 1000 USDT
- Capital per trade: 15% NAV
- Max positions: 3
- Stop loss: 10%
- Take profit: 25%
- Trailing stop: 12%
- Max hold: 72h
- Cooldown: 24h per symbol

## Production Path

1. Complete chronological backtest and inspect trade distribution by regime/month/symbol.
2. Run walk-forward training and threshold stability checks.
3. Run paper mode with:

```bash
python3 scripts/run_monster_paper.py --state-id monster_v1 --refresh --loop
```

4. Add formal risk monitor before any order placement: max daily loss, max concurrent correlated alts, stale data kill switch, spread/depth kill switch.
5. Only after approval, implement execution through `okx swap place` or Agent Trade Kit wrapper.

## Current Caveats

- The classifier is trained on labels mined from the same historical universe, so it may overfit microcap pump structure.
- Historical liquidity quality is incomplete; live orderbook gates are only available for current scans.
- OI/funding/long-short history is not yet long enough for robust monster labels.
- Current model is a scoring heuristic, not a calibrated probability model.
