# Feature Pipeline Plan — 2026-04-24

## Diagnosis

The full research-to-production pipeline is not complete yet.

Current engine strengths:
- Live strategy execution exists.
- Backtest machinery exists.
- Risk controls exist.
- OKX profile isolation is now much better than before.

Main missing institutional quant pieces:
- Formal data catalog and dataset versioning.
- Point-in-time feature store with offline/live parity.
- Label/target builder independent from strategy code.
- Feature validation and leakage checks.
- Feature selection with walk-forward and purged CV.
- Automated promotion path from selected features to strategy configs.
- Production monitoring that checks feature drift, signal drift, execution slippage, and risk state by environment.

## First Layer Implemented

Added:
- `engine/features/builders.py`
- `engine/features/labels.py`
- `engine/features/validation.py`
- `engine/features/selection.py`
- `engine/research/feature_pipeline.py`

Smoke run:
- Dataset: `engine/data/features/smoke_1h_mar2026/`
- Symbols: `BTC/USDT`, `ETH/USDT`, `SOL/USDT`
- Timeframe: `1h`
- Range: `2026-03-01` to `2026-03-31`
- Result: validation `ok`, 2232 aligned rows, 40 features, 25 labels.

## Target Automated Pipeline

1. Data ingestion
   - OHLCV, funding, open interest, order book, trades, liquidation/stress proxies, long/short ratio, account fills, fees, and slippage.
   - Persist immutable raw datasets.

2. Feature store
   - One row per `(timestamp, symbol)`.
   - All features must be point-in-time safe.
   - Every feature has metadata: source, lookback, frequency, live availability, and expected NaN window.

3. Label builder
   - Forward returns by horizon.
   - Direction labels.
   - MFE/MAE.
   - Barrier labels for small-account high-risk strategies.
   - Cost-adjusted labels using fee and slippage assumptions.

4. Validation
   - Duplicate index checks.
   - NaN/inf thresholds.
   - All-NaN feature rejection.
   - Staleness and timestamp-gap checks.
   - Leakage checks: labels cannot join into feature columns; rolling windows cannot look forward.

5. Feature selection
   - Cross-sectional IC and time-series IC.
   - Stability by regime.
   - Turnover and cost sensitivity.
   - Purged walk-forward CV.
   - Reject features that only work in one month or one asset.

6. Strategy formation
   - Convert stable features into signal modules.
   - Combine signals with regime gates.
   - Size positions by expected edge, volatility, drawdown state, and account-level exposure.

7. Backtest and simulation
   - Replay point-in-time features.
   - Include fees, slippage, funding, min contract sizes, liquidation distance, and order rejection.
   - Monte Carlo remains useful, but it should sit after deterministic walk-forward backtests.

8. Production
   - Use the same feature definitions live.
   - Store live feature snapshots and decisions.
   - Tag every order with environment, strategy, dataset version, and signal version in local logs.

9. Monitoring
   - Environment-separated NAV, positions, fills, summary, and decision logs.
   - Feature drift and missing data alerts.
   - Signal drift alerts.
   - Slippage and fill-quality tracking.
   - Kill switches by strategy and by OKX profile.

## Next Build Order

1. Add feature registry metadata and dataset manifests.
2. Add order book/trade/open-interest feature builders.
3. Add purged walk-forward feature selection.
4. Add cost-adjusted labels.
5. Add strategy candidate generator that writes backtest configs from selected feature sets.
6. Add production feature snapshot logging.
7. Add environment-specific dashboard summaries.
