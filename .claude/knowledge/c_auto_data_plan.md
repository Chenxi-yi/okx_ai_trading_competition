# C-Auto Data Download Plan

Last updated: 2026-05-05T16:51:00+0800

## Goal

Build a point-in-time market dataset for `core_c_auto_h24_regression_v1`.
C-Auto is ML-driven, so data quality, cross-sectional coverage, and leakage
control matter more than adding strategy logic early.

## Interfaces

### OKX Agent Trade Kit

Best for runtime sensors and execution-adjacent checks:

- current ticker
- current candles
- current orderbook
- current funding rate
- current open interest
- account balance, positions, bills, fills
- order placement/cancel/close through `okx swap place`

Do not use Kit for large historical training downloads. It is a local trading
I/O layer, not a bulk research data source.

### OKX Public API via ccxt

Best for historical training data:

- OHLCV candles at 5m, 15m, 1h, 1d
- volume and turnover from candles/tickers
- historical funding rate
- historical open interest
- historical long/short ratio
- instrument metadata/list time/contract size
- limited recent trades/orderbook snapshots for live feature calibration

## Useful Model Inputs

Core price/volume:

- open, high, low, close
- base volume
- quote volume / dollar turnover
- return over 5m, 15m, 1h, 4h, 24h
- realized volatility and range
- volume z-score, volume acceleration, dollar-volume rank
- cross-sectional momentum and reversal ranks

Derivatives structure:

- funding rate
- funding rate z-score and rolling mean
- open interest amount
- open interest notional/value
- OI change over 5m, 1h, 24h
- OI/volume ratio
- long/short ratio
- long/short ratio change and extremes

Market quality:

- 24h quote volume
- spread snapshot
- top-book depth snapshot
- listing time / age
- missing bar ratio
- stale data flags
- max gap length

Runtime-only calibration:

- latest ticker
- latest orderbook
- latest trades
- account/position/bill reconciliation

## Universe

Rule:

- OKX active USDT-margined perpetual swaps
- exclude stablecoins, index-like contracts, and TradFi perps
- `24h quote volume >= 5,000,000 USDT`
- cap `max_symbols=220`

Discovery result on 2026-05-05:

- 79 symbols passed the 5M filter
- the user expected about 150-200, but the live OKX filter returned 79 at this threshold

Universe manifest:

```text
engine/data/training_history/c_auto_universe_vol5m_5m_15m_20240101_20260505/manifest.json
```

## Download Runs

Phase 1: OHLCV/volume

```text
run_id: c_auto_universe_vol5m_5m_15m_20240101_20260505
source: OKX public via ccxt
symbols: 79
timeframes: 5m,15m
date range: 2024-01-01 through 2026-05-05
jobs: 158
output: engine/data/cache/*_futures_5m.parquet and *_futures_15m.parquet
progress: engine/data/training_history/<run_id>/progress.jsonl
```

Phase 2: derivatives structure

```text
run_id: c_auto_deriv_vol5m_5m_20240101_20260505
source: OKX public via ccxt
symbols: same 79-symbol universe
kinds: funding,open_interest,long_short
timeframe: 5m
date range: 2024-01-01 through 2026-05-05
jobs: 237
output: engine/data/derivatives_structure/<run_id>/*/*.parquet
progress: engine/data/derivatives_structure/<run_id>/progress.jsonl
```

Phase 3: higher-timeframe OHLCV/volume

```text
run_id: c_auto_universe_vol5m_1h_4h_1d_20240101_20260505
source: OKX public via ccxt
symbols: same 79-symbol universe
timeframes: 1h,4h,1d
date range: 2024-01-01 through 2026-05-05
jobs: 237
output: engine/data/cache/*_futures_1h.parquet, *_futures_4h.parquet, *_futures_1d.parquet
progress: engine/data/training_history/<run_id>/progress.jsonl
purpose: multi-scale direct features and validation against 5m resampling
```

Phase 4: market-quality snapshot

```text
run_id: c_auto_market_quality_snapshot_20260505
source: OKX public via ccxt
symbols: same 79-symbol universe
kinds: instrument,ticker,orderbook,trades
timeframe: snapshot
jobs: 316
output: engine/data/derivatives_structure/<run_id>/*/*.parquet
progress: engine/data/derivatives_structure/<run_id>/progress.jsonl
purpose: listing age, contract metadata, current spread/depth, recent trade microstructure
```

Notes:

- Historical orderbook depth and full tick/trade history are not available
  through the current broad-download path at practical cost. Use snapshot
  quality data for live calibration and liquidity filters.
- `open_interest` can return OKX `Illegal time range` for some symbols/date
  windows. Keep successful OI files, then repair missing symbols with shorter
  backfill windows if the first broad pass leaves failures.

## Feature Pipeline Artifacts

Quality dataset:

```text
dataset_id: c_auto_dataset_quality_v1
output: engine/data/quality/c_auto_dataset_quality_v1/
symbols: 79
core-ready symbols: 79
train eligible 90d: 72
train eligible 180d: 67
status: warn
reason: 10 higher-timeframe jobs are short-history failures for new/listed-late symbols
```

Feature store:

```text
dataset_id: c_auto_feature_store_v1
output: engine/data/features/c_auto_feature_store_v1/
rows: 1,087,371
features: 66
labels: 40
frequency: 1h
walk-forward folds: 54
validation: warn:excessive_feature_nan
reason: derivatives/OI/long-short history is naturally shorter than OHLCV
```

Point-in-time rule:

- Historical feature rows may use OHLCV, funding, OI, long/short, higher
  timeframe bars, contract metadata, listing age, and quality flags.
- Current orderbook/ticker/trades snapshots must not be spread backward across
  historical rows. Use them only for latest live liquidity filters and execution
  gates.

First baseline:

```text
experiment_id: c_auto_feature_store_v1_baseline_12fold
backend: fallback_linear_score (local sklearn/scipy unavailable)
folds: 12
spearman_ic: -0.0428
directional_accuracy: 0.5095
long_short_spread: -0.00130
verdict: default legacy feature set is not promotable; build a new feature set
from IC-ranked multi-timeframe/regime features before backtesting.
```

## Frontend Progress

Launcher download widget reads:

- `manifest.json`
- `status.json`
- `progress.jsonl`
- active `scripts/fetch_training_history.py` or `scripts/fetch_derivatives_structure.py` process

Pause uses `POST /api/download-pause`.
Resume uses `POST /api/download-resume`.

Launcher now chooses the active run first, then the newest manifest, so c-auto
runs are visible instead of falling back to older default data runs.
