# Session Log

Purpose: persistent handoff log for Codex work on this trading bot. Read this file at the start of a new session before continuing architecture, data, feature, or execution work.

## 2026-04-24 11:35 CST — Project Architecture Review Started

User objective:
- Review the whole OKX trading bot as a high-risk, high-return quantitative system for about 1000 USDT capital.
- Identify architecture gaps, missing data, and ways to discover more quantitative features.
- Maintain a durable progress log because model sessions may end or quota may run out.

Current status:
- Working directory: `/Users/yichenxi/Desktop/okx_ai_skill_challenage`.
- Engine status command in `AGENTS.md` pointed to `.Codex/tools/trading_status.py`, but actual tools are under `.claude/tools/`.
- Actual status command used: `python3 .claude/tools/trading_status.py`.
- Engine reported running with PID `73327`; `yolo_orchestrator` NAV about `49.13` on `50.00` capital, PnL about `-1.74%`, one position.
- Error logged to `.claude/errors/registry.jsonl` with code `DOC_PATH_DRIFT`.

Review focus for this session:
- Map architecture and runtime boundaries.
- Inspect data layer, backtest layer, strategy implementations, risk/execution, dashboard/logging.
- Produce a prioritized gap analysis and a concrete roadmap for feature engineering and data expansion.

Next actions:
- Read core strategy and data/backtest/risk/execution modules.
- Identify where feature storage, feature validation, offline research, and live parity are missing.
- Propose a small-cap high-risk research plan with controls for liquidation, fee drag, and overfit.

Progress update:
- Created `.claude/ARCHITECTURE_REVIEW_2026-04-24.md` with first-pass findings.
- Confirmed OKX public endpoints are callable via `okx`: tickers, BTC order book, funding, and open interest.
- Observed `okx --version` reports `1.2.7`, with update notice for newer CLI releases.
- Key diagnosis: no first-class feature store or label layer; highest-risk live strategy loops are not fully unified with the cleaner backtest/live pipeline.

Recommended next session start:
- Read `.claude/ARCHITECTURE_REVIEW_2026-04-24.md`.
- Implement `engine/features/` and `engine/research/` scaffolding before tuning more strategy parameters.

## 2026-04-24 — Environment Isolation Review and Patch

User clarified OKX environments:
- `demo`
- `live` = competition
- `personal`

Changes made:
- Added explicit OKX profile validation and profile discovery in `engine/config/settings.py`.
- Fixed `engine/main.py` custom strategy launch bug where `effective_config` was built but not passed to `mod.run()`.
- Added `--profile` to `engine/main.py competition demo-start`.
- Added `--okx-profile` to `engine/main.py start`.
- Changed `Broker` and `TradingEngine` to carry explicit OKX profile instead of mapping all non-demo calls to `live`.
- Updated `elite_flow`, `yolo_momentum`, and `yolo_orchestrator` to accept any configured OKX profile.
- Updated local scripts to accept `personal` and write environment-specific PID/state files.
- Added full review doc: `.claude/CODE_REVIEW_ENV_ISOLATION_2026-04-24.md`.

Verification:
- Python compile check passed with `PYTHONPYCACHEPREFIX=/tmp/okx_pycache`.
- `zsh -n` passed for `start_local.sh`, `stop_local.sh`, and `manage_local.sh`.
- Profile discovery confirmed `demo`, `live`, and `personal` are visible and populated after fallback parsing.

Remaining:
- Fixed `~/.okx/config.toml` keys to `api_key` and `secret_key` for all profiles so the OKX CLI can parse them reliably.
- `live` and `personal` private endpoint smoke checks succeeded.
- `demo` private endpoint smoke check failed with `HTTP 401 Invalid OK-ACCESS-KEY`; logged as `DEMO_PROFILE_AUTH_INVALID`.
- `logs/summary.json` is still shared; build profile-specific summary files before running multiple environments concurrently.

## 2026-04-24 12:07 CST — Feature Store / Label Builder v0

User objective:
- Build the missing formal feature store, label/target builder, and feature validation layer.
- Move toward an automated quant pipeline: data -> features -> feature selection -> strategy -> backtest -> production -> risk monitoring.

Changes made:
- Added `engine/features/`:
  - `builders.py`: point-in-time OHLCV/funding feature panel.
  - `labels.py`: forward-return, direction, MFE/MAE, and barrier-style labels.
  - `validation.py`: schema/NaN/inf/duplicate validation report.
  - `selection.py`: cross-sectional IC summary without optional scipy dependency.
- Added `engine/research/feature_pipeline.py`:
  - Fetches universe data through existing `data.fetcher.fetch_universe`.
  - Materializes `features.parquet`, `labels.parquet`, `ic_summary.parquet`, `validation.json`, and `metadata.json` under `engine/data/features/<dataset-id>/`.

Smoke dataset:
- Command: `PYTHONPYCACHEPREFIX=/tmp/okx_pycache python3 engine/research/feature_pipeline.py --symbols BTC/USDT,ETH/USDT,SOL/USDT --start 2026-03-01 --end 2026-03-31 --timeframe 1h --dataset-id smoke_1h_mar2026 --label-col fwd_ret_6`
- Output: `engine/data/features/smoke_1h_mar2026/`
- Result: `validation_status=ok`, `n_rows=2232`, `n_features=40`, `n_labels=25`.
- Top smoke IC features against `fwd_ret_6`: `ret_6`, `mom_z_24`, `ret_12`, `mom_z_6`, `ret_3`. This is a tiny universe/month smoke test, not a production edge claim.

Errors resolved:
- `FEATURE_PIPELINE_SCIPY_DEP`: pandas Spearman correlation pulled in scipy, which is not installed. Fixed with rank-based Pearson Spearman IC.

Next actions:
- Extend feature inputs beyond OHLCV/funding: order book imbalance, trades/aggressor flow, open interest, long/short ratio, liquidation/proxy stress, cross-asset beta, BTC regime, basis/funding term structure.
- Add walk-forward splitter and purged CV to prevent leakage.
- Add feature registry metadata with owner, inputs, lookback, expected frequency, and live availability.
- Wire selected features into strategy candidate generation and backtest configs.
- Add profile-specific runtime summaries before running multiple environments concurrently.

## 2026-04-24 12:17 CST — Feature Pipeline v1

Completed Phase 1 research-data backbone:
- Added feature registry metadata in `engine/features/registry.py`.
- Added dataset manifest helper in `engine/research/manifest.py`.
- Extended validation to report feature registry coverage, per-feature NaN rates, excessive NaN features, timestamp gaps, and symbols with gaps.
- Added cost-adjusted labels: net long return, net short return, and absolute edge after cost assumptions.
- Added purged walk-forward fold generation in `engine/research/walk_forward.py`.
- Updated `engine/research/feature_pipeline.py` to write:
  - `features.parquet`
  - `labels.parquet`
  - `ic_summary.parquet`
  - `validation.json`
  - `feature_registry.json`
  - `label_registry.json`
  - `walk_forward_folds.json`
  - `metadata.json`
  - `manifest.json`

Smoke dataset:
- Command used label `fwd_ret_net_long_6` with `fee_bps=5`, `slippage_bps=2`, `funding_cost_bps_per_bar=0`.
- Output: `engine/data/features/smoke_1h_mar2026_pipeline_v1/`
- Result: `validation_status=ok`, `n_rows=2232`, `n_features=40`, `n_labels=40`, registry coverage `1.0`, timestamp gaps `0`, walk-forward folds `5`.

Next actions:
- Phase 2 starts with non-OHLCV alpha data: order book, trade flow, open interest, funding/basis stress, cross-asset regime, and volatility/liquidation proxies.
- Then wire fold-level IC/stability scoring into feature selection.

## 2026-04-24 13:42 CST — Microstructure Data Collector v0

User approved writing Python fetchers for the next data layer.

Changes made:
- Added `engine/data/microstructure.py`.
  - Public OKX/ccxt read-only data collection.
  - Supports `ticker`, `instrument`, `ohlcv`, `funding`, `open_interest`, `long_short`, `trades`, and `orderbook`.
  - Writes versioned datasets under `engine/data/microstructure/<dataset-id>/<symbol>/`.
  - Writes `manifest.json` with artifacts, row counts, per-kind status, and SHA256 fingerprints.
- Added `scripts/fetch_microstructure.py` CLI.
- Added `engine/features/microstructure.py`.
  - Builds a `(timestamp, symbol)` microstructure feature panel from persisted artifacts.
  - First features include orderbook spread/depth imbalance, trade-flow imbalance, OI changes, funding changes, and long/short-ratio changes.

Smoke fetches:
- `smoke_micro_btc_now_v2`: BTC ticker, instrument metadata, and 5-level orderbook snapshot succeeded.
- `smoke_micro_btc_series`: BTC funding, open interest, long/short ratio, and trades succeeded.
- `smoke_micro_btc_ohlcv_1m`: BTC 1m OHLCV succeeded with 300 rows.

Resolved issues:
- `MICROSTRUCTURE_ORDERBOOK_LEVEL_WIDTH`: OKX orderbook levels can contain more than price/size. Parser now takes the first two fields.

Next actions:
- Fetch a small multi-symbol dataset for BTC/ETH/SOL with all kinds.
- Turn microstructure features into registry specs and merge them with the existing feature pipeline.
- Add richer orderbook/trade-flow features and fold-level IC scoring.

## 2026-04-24 13:54 CST — End-to-End Feature + Microstructure Pipeline

Completed the first full flow:
- Microstructure dataset:
  - `engine/data/microstructure/micro_core_btc_eth_sol_smoke_v2/`
  - Symbols: `BTC/USDT`, `ETH/USDT`, `SOL/USDT`
  - Kinds: `ohlcv`, `ticker`, `instrument`, `funding`, `open_interest`, `long_short`, `trades`, `orderbook`
  - Result: all requested kinds succeeded for all 3 symbols.
- Feature dataset:
  - `engine/data/features/smoke_feature_plus_micro_v2/`
  - Built 1m OHLCV/funding features plus 20 microstructure features.
  - Result: `n_rows=1062`, `n_features=60`, `n_labels=40`, registry coverage `1.0`, timestamp gaps `0`, walk-forward folds `7`.
  - Validation status: `warn:excessive_feature_nan`.
  - The warning is expected for this smoke run because orderbook used only 2 snapshots and trades used a small latest-trades sample. The pipeline correctly emits a warning instead of hiding sparse coverage.

Resolved issues:
- `MICROSTRUCTURE_STATS_TIMEFRAME`: OKX OI and long/short stats reject `1m`; collector now falls back to `5m` for stats endpoints while keeping OHLCV at `1m`.
- `FETCHER_1M_PANDAS_FREQ`: pandas interpreted `1m` as month-end; fetcher now maps ccxt minute strings like `1m` to pandas `1min`.
- `MICRO_FEATURE_OUTER_JOIN_GAPS`: micro features were initially outer-joined and created sparse timestamp gaps; now they are point-in-time aligned to the base OHLCV index.

Next actions:
- Pull longer microstructure windows with enough orderbook/trade samples to reduce NaN coverage.
- Add richer derived features: orderbook slope/replenishment, trade burst, CVD, OI-price divergence, funding stress, BTC/ETH regime features.
- Add fold-level IC/stability scoring before strategy generation.

## 2026-04-24 13:58 CST — Checkpoint Before Long Download

User asked to pause feature work because model quota/window may expire soon, archive progress, and start downloading broader historical data for model training.

Current completed pipeline state:
- Feature store v1 works.
- Label builder includes cost-adjusted labels.
- Validation includes registry coverage, NaN, inf, duplicate index, and timestamp-gap checks.
- Microstructure collector works for OKX public read-only data.
- End-to-end feature + microstructure smoke pipeline works:
  - Micro dataset: `engine/data/microstructure/micro_core_btc_eth_sol_smoke_v2/`
  - Feature dataset: `engine/data/features/smoke_feature_plus_micro_v2/`
  - Result: 60 features, 40 labels, registry coverage 1.0, timestamp gaps 0.

Long download plan:
- Discover current OKX USDT swap universe with 24h quote volume >= 30,000,000 USDT.
- Start with broad 1h OHLCV + funding history for model training.
- Store existing fetcher cache under `engine/data/cache/`.
- Write run manifests/progress under `engine/data/training_history/`.
- Make the job resumable so a later session can continue without restarting completed symbols.

Recommended after quota/window reset:
- Check `engine/data/training_history/<run-id>/progress.jsonl`.
- Check the background process PID and log file recorded below.
- Then add richer 5m/1m slices for only the liquid/core universe or recent windows.

## 2026-04-24 16:28 CST — Training History Download Started In Current Project

User corrected that data must stay in the current Desktop project, not `.openclaw`.

Important:
- The temporary `.openclaw` LaunchAgent was stopped.
- Do not continue writing new research data to `.openclaw`.
- Current canonical project path is `/Users/yichenxi/Desktop/okx_ai_skill_challenage`.

Downloader:
- Script: `scripts/fetch_training_history.py`
- Run id: `train_hist_vol1m_1h_20240101_20260424`
- Universe threshold: current 24h quote volume >= 1,000,000 USDT.
- Universe size at discovery: 134 symbols.
- Timeframe: `1h`.
- Date range: `2024-01-01` through `2026-04-24`.
- Current running tool session id: `86144`.

Output paths:
- Manifest: `engine/data/training_history/train_hist_vol1m_1h_20240101_20260424/manifest.json`
- Progress: `engine/data/training_history/train_hist_vol1m_1h_20240101_20260424/progress.jsonl`
- OHLCV/funding cache: `engine/data/cache/`

Safety notes:
- The downloader does not delete existing data.
- Progress is append-only JSONL.
- Successful `(symbol, timeframe)` jobs are skipped on rerun.
- `fetch_ohlcv` now requires cache to cover requested start and end before using it.
- Training downloader disables stale-cache/yfinance fallback to avoid polluted training data.

Current status at checkpoint:
- Download is running in the current project directory.
- BTC/USDT completed successfully:
  - rows: 20265
  - first: `2024-01-01 00:00:00+00:00`
  - last: `2026-04-24 08:00:00+00:00`
  - coverage: `0.99926`

Progress update at 2026-04-24 16:39 CST:
- Download session `86144` is still running.
- Progress records: 5 total.
- Successful:
  - `BTC/USDT`
  - `ETH/USDT`
  - `YFI/USDT`
  - `BNB/USDT`
- Failed:
  - `ZEC/USDT`: OKX returned no OHLCV data for the requested historical range after retries.
- Current console output shows the job moved on to `BZ/USDT`.
- Safety status: no deletes; progress is append-only; failed symbols are recorded, not silently replaced by stale/yfinance data.

Progress update at 2026-04-24 17:12 CST:
- User asked to pause and clarify data sources.
- Download session `86144` was interrupted with Ctrl-C and is no longer running.
- Progress records at pause: 8 total.
- Successful:
  - `BTC/USDT`
  - `ETH/USDT`
  - `YFI/USDT`
  - `BNB/USDT`
- Failed:
  - `ZEC/USDT`
  - `BZ/USDT`
  - `TAO/USDT`
  - `CL/USDT`
- The failure pattern is `No OHLCV data returned`; this should be made a fast-fail condition before resuming so the downloader does not spend about 9 minutes per unsupported ticker.

Update at 2026-04-24 17:16 CST:
- Implemented fast-fail in `scripts/fetch_training_history.py`.
- Errors containing `No OHLCV data returned`, `Symbol not found`, `does not have market symbol`, or `market not found` now stop retrying immediately.
- Rate-limit style errors can still use retry/backoff.
- Compile check passed.

Update at 2026-04-24 18:08 CST:
- User questioned whether failed symbols were actually missing, because they are visible in the OKX app.
- Confirmed the issue was not app availability. OKX/ccxt returns no OHLCV when `since` is earlier than the swap contract listing time for some newer contracts.
- Fixed `engine/data/fetcher.py`:
  - If initial OHLCV fetch returns empty, check market `listTime`.
  - Retry from `listTime` when it is after requested start and before requested end.
- Verified examples now work from listing time:
  - `ZEC/USDT`: 4063 rows, starts `2025-11-06 04:00 UTC`
  - `TAO/USDT`: 13949 rows, starts `2024-09-20 06:00 UTC`
  - `DASH/USDT`: 4063 rows, starts `2025-11-06 04:00 UTC`
  - `HYPE/USDT`: 10251 rows, starts `2025-02-21 08:00 UTC`
- Before resuming download, consider whether previous failed records should be retried or filtered. Current downloader only skips `ok`, so retrying will pick them up with the fixed fetcher.

Update at 2026-04-24 18:30 CST:
- User asked why there was no visible progress after interruption.
- No new long-running download was started after the interruption.
- Compile check still passes for `engine/data/fetcher.py` and `scripts/fetch_training_history.py`.
- Current progress file:
  - records: 39
  - ok: 19
  - failed: 20
  - last record: `BERA/USDT failed`
- Previous failed records can now be retried because `fetcher.py` handles list-time fallback.

Rerun / resume command:
```bash
cd /Users/yichenxi/Desktop/okx_ai_skill_challenage
PYTHONPYCACHEPREFIX=/tmp/okx_pycache python3 scripts/fetch_training_history.py \
  --run-id train_hist_vol1m_1h_20240101_20260424 \
  --min-volume-usd 1000000 \
  --max-symbols 300 \
  --start 2024-01-01 \
  --end 2026-04-24 \
  --timeframes 1h \
  --sleep-sec 4 \
  --retry-attempts 8 \
  --retry-sleep-sec 20 \
  --min-rows 100
```

## 2026-04-24 21:55 CST — Local Trading Launcher UI Added

User objective:
- Add a clickable frontend launcher so the trading system can be started without typing shell commands.
- Launcher should choose environment (`demo`, `live` competition, `personal`), choose strategy, start the existing system, and show the already-built YOLO monitoring page.

Changes made:
- Added local launcher server:
  - `launcher/launcher_server.py`
  - Thin stdlib HTTP server; validates requests and delegates lifecycle work to existing `manage_local.sh`.
  - `POST /api/start` calls `./manage_local.sh start <strategy> <port> <env>`.
  - `POST /api/stop` calls `./manage_local.sh stop`.
  - `GET /api/status` reports root path, dashboard/launcher pid state, strategy pid files, `engine/logs/summary.json`, and recent launcher logs.
- Added launcher frontend:
  - `launcher/static/index.html`
  - `launcher/static/style.css`
  - `launcher/static/app.js`
  - Provides environment selection, strategy selection, dashboard port, start/stop/refresh controls, runtime pid/status cards, launcher log tail, and an iframe for `http://127.0.0.1:<port>/yolo`.
- Added double-click macOS entry file:
  - `Start Trading Launcher.command`
  - Starts launcher on port `8788` if needed, writes `engine/control/launcher.pid`, and opens `http://127.0.0.1:8788/`.

Safety:
- `live` / competition startup requires explicit confirmation in the UI and is also rejected by the backend unless `confirm_live=true`.
- The launcher does not implement trading logic or place orders directly; it only calls the existing local scripts.
- No data downloader files were deleted or modified by this launcher work.

Verification:
- `PYTHONPYCACHEPREFIX=/tmp/okx_pycache python3 -m py_compile launcher/launcher_server.py` passed.
- `zsh -n 'Start Trading Launcher.command'` passed.
- Launcher server started locally on `http://127.0.0.1:8788/`.
- `GET /api/status` returned OK.
- Static homepage returned OK.
- `POST /api/start` with `env=live` and no confirmation returned:
  - `{"ok": false, "error": "live/competition environment requires explicit confirmation"}`

Usage:
- Double-click `Start Trading Launcher.command` from Finder.
- Or open `http://127.0.0.1:8788/` if the launcher is already running.

## 2026-04-24 22:38 CST — Download Monitor Widget Added

User objective:
- Add a small visible window for data downloads showing expected count, completed count, current download target, and pause/continue controls.
- Make this the default monitor for future training-history downloads.

Changes made:
- Extended `launcher/launcher_server.py`:
  - `GET /api/download-status` reads `engine/data/training_history/<run-id>/manifest.json` and append-only `progress.jsonl`.
  - `POST /api/download-pause` terminates only matching `scripts/fetch_training_history.py` processes for the selected run id.
  - `POST /api/download-resume` restarts the same run id from manifest parameters; existing successful jobs are skipped by the downloader.
  - Pause/resume do not delete cache or progress files.
- Extended launcher UI:
  - Added fixed bottom-right “数据下载” widget in `launcher/static/index.html`.
  - Updated `launcher/static/style.css` for progress bar, counters, current/next symbol, and buttons.
  - Updated `launcher/static/app.js` to poll download status every 5 seconds and wire pause/continue buttons.
- Extended `scripts/fetch_training_history.py`:
  - Writes `engine/data/training_history/<run-id>/status.json` before each symbol starts and after each record finishes.
  - Future resumed downloads will expose the current symbol more accurately than inferring from the last completed progress record.

Verification:
- `PYTHONPYCACHEPREFIX=/tmp/okx_pycache python3 -m py_compile launcher/launcher_server.py scripts/fetch_training_history.py` passed.
- Launcher restarted on `http://127.0.0.1:8788/`.
- `GET /api/download-status` returned OK.
- Current observed download status at verification:
  - run id: `train_hist_vol1m_1h_20240101_20260424`
  - total jobs: `134`
  - downloaded: `95`
  - latest failed: `1`
  - remaining: `39`
  - running process: PID `13243`

Note:
- The existing running downloader was started before `status.json` heartbeat support, so the widget currently infers current symbol from the last completed record until the next resume. After pause/resume or future runs, `status.json` will provide exact current-symbol heartbeats.

## 2026-04-24 23:18 CST — Training History Download Completed

Observed status:
- Run id: `train_hist_vol1m_1h_20240101_20260424`
- Path: `engine/data/training_history/train_hist_vol1m_1h_20240101_20260424`
- Manifest status: `completed`
- Download process: not running
- Total jobs: `134`
- Latest successful jobs from progress JSONL: `132`
- Latest failed jobs: `2`
  - `OPG/USDT`: insufficient history coverage rows=`30`, min_rows=`100`
  - `CHIP/USDT`: insufficient history coverage rows=`73`, min_rows=`100`
- Manifest summary says `ok=113`, `skipped_existing=19`, `failed=2`; by latest per-symbol progress, total usable ok jobs are `132`.
- Cache size: `engine/data/cache` about `113M`, 201 cache files.
- Sample BTC cache confirmed columns: `open`, `high`, `low`, `close`, `volume`, `funding_rate`.

Coverage profile:
- Coverage >= 95%: 60 symbols
- Coverage >= 80%: 66 symbols
- Coverage >= 50%: 85 symbols
- Coverage >= 20%: 106 symbols
- Coverage >= 10%: 114 symbols
- Low-coverage symbols are mostly newly listed contracts/stocks/commodities proxies, not necessarily bad data.

Recommended next data batches:
- Start with 5m OHLCV+funding for the liquid/core universe, not all 132 symbols.
- Add derivatives microstructure history for the same core set: open interest, long/short ratio, funding history, recent trades, and orderbook snapshots.
- Add 1m recent-window data only for the top/core symbols because all-symbol 1m from 2024 would be slow and heavy.

## 2026-04-24 23:46 CST — Full 5m Derivatives Structure Download Started

User decision:
- Download the full 5-minute derivatives structure dataset for the full 132/134-symbol universe, even if it takes a long time.
- Data foundation quality takes priority over speed.

Changes made:
- Added resumable downloader: `scripts/fetch_derivatives_structure.py`
  - Output root: `engine/data/derivatives_structure/<run-id>/`
  - Writes `manifest.json`, append-only `progress.jsonl`, and live `status.json`.
  - Supports `funding`, `open_interest`, and `long_short`.
  - Per `(symbol, kind, timeframe)` success is skipped on resume.
- Updated launcher download monitor:
  - `launcher/launcher_server.py` now detects both `training_history` and `derivatives_structure` runs.
  - Default tracked derivatives run id is `deriv_struct_132_5m_20240101_20260424`.
  - UI now shows `symbol / kind` for current and next item.

Resolved issue:
- `DERIV_STRUCT_MANIFEST_RELATIVE_PATH`: fixed relative `--symbols-manifest` handling by resolving before `relative_to(ROOT)`.

Smoke test:
- `deriv_struct_smoke_btc_5m` with BTC only succeeded:
  - funding rows: `5`
  - open_interest rows: `477`
  - long_short rows: `20`

Long-running job started:
```bash
python3 scripts/fetch_derivatives_structure.py \
  --run-id deriv_struct_132_5m_20240101_20260424 \
  --symbols-manifest engine/data/training_history/train_hist_vol1m_1h_20240101_20260424/manifest.json \
  --start 2024-01-01 \
  --end 2026-04-24 \
  --timeframe 5m \
  --kinds funding,open_interest,long_short \
  --limit 100 \
  --sleep-sec 1 \
  --retry-attempts 4 \
  --retry-sleep-sec 8
```

Current initial status:
- Process PID: `20073`
- Tool session id in this Codex turn: `49299`
- Total jobs: `402` (`134 symbols * 3 kinds`)
- First completed job: `BTC/USDT funding`, rows=`280`
- Current item at checkpoint: `BTC/USDT open_interest`
- Monitor URL: `http://127.0.0.1:8788/`

## 2026-04-25 13:45 CST — 5m Derivatives Structure OI Retry

User observed:
- `deriv_struct_132_5m_20240101_20260424` completed with `268` ok and `134` failed.

Diagnosis:
- All successful jobs were:
  - `funding`: 134/134 ok
  - `long_short`: 134/134 ok
- All failed jobs were:
  - `open_interest`: 134/134 failed
- Error was identical for every OI job:
  - OKX `50030 Illegal time range`
- Cause: OKX/ccxt open-interest history endpoint rejects a very long 5m `begin` range. It needs bounded `begin`/`end` windows.

Fix:
- Updated `scripts/fetch_derivatives_structure.py`.
- `_fetch_open_interest()` now paginates backward with bounded `begin`/`until` windows instead of querying `2024-01-01 -> 2026-04-24` in one request.
- Smoke test passed:
  - run id: `deriv_struct_oi_smoke_btc_5m`
  - `BTC/USDT open_interest`
  - rows: `507`

Resume:
- Restarted the same run id:
  - `deriv_struct_132_5m_20240101_20260424`
- Existing successful `funding` and `long_short` jobs are skipped.
- Previously failed `open_interest` jobs are being retried.
- Current process PID at checkpoint: `44205`
- Current tool session id at checkpoint: `41802`
- Monitor URL: `http://127.0.0.1:8788/`
- Early retry progress confirmed:
  - `BTC/USDT open_interest`: ok, rows=`507`
  - `ETH/USDT open_interest`: ok, rows=`507`
  - `ZEC/USDT open_interest`: ok, rows=`507`
  - `YFI/USDT open_interest`: ok, rows=`507`

Important data note:
- OKX may only expose a limited recent history window for 5m open-interest data. The retry now downloads the maximum available window per symbol instead of failing the whole kind.

## 2026-04-25 13:58 CST — Derivatives Structure Coverage Check

Current completed status:
- Run id: `deriv_struct_132_5m_20240101_20260424`
- Latest status by `(symbol, kind, timeframe)`:
  - `funding`: 134/134 ok
  - `open_interest`: 134/134 ok
  - `long_short`: 134/134 ok
  - total: 402/402 ok
- Output size: about `5.7M`

Actual coverage:
- `open_interest`
  - Frequency: true 5-minute interval.
  - Files: 134.
  - Rows per symbol: 500 to 507.
  - Earliest timestamp range: `2026-04-23 05:45 UTC` to `2026-04-23 06:20 UTC`.
  - Latest timestamp: `2026-04-24 23:55 UTC`.
  - Practical coverage: about `1 day 17h35m` to `1 day 18h10m`.
  - Interpretation: OKX endpoint appears to expose only a short recent 5m OI window through this ccxt path.
- `long_short`
  - Frequency: true 5-minute interval.
  - Rows per symbol: exactly 100.
  - Earliest timestamp range: `2026-04-24 07:25 UTC` to `2026-04-24 09:25 UTC`.
  - Latest timestamp range: `2026-04-24 15:40 UTC` to `2026-04-24 17:40 UTC`.
  - Practical coverage: about `8h15m`.
- `funding`
  - Not actually 5m; funding uses exchange funding settlement cadence.
  - Interval modes: 1h, 4h, or 8h depending on instrument.
  - Rows per symbol: 8 to 1515.
  - Longest duration: about `93 days 8h`.

Next recommended data:
- 5m OHLCV for the same 134-symbol universe. We currently have broad 1h OHLCV/funding, not broad 5m OHLCV.
- 1m or 5m trades/orderbook snapshots for the core liquid universe, not necessarily all 134 at first.
- If deeper historical OI/long-short is required, test OKX native REST endpoints directly rather than ccxt wrappers; ccxt appears limited for these endpoints.

## 2026-04-25 15:57 CST — Full 134-Symbol 5m OHLCV Download Started

User decision:
- Download broad 5-minute OHLCV for the same 134-symbol universe.
- Keep the launcher download widget connected to this new run.
- Continue date-stamped logging for handoff.

Changes made:
- Updated `scripts/fetch_training_history.py`:
  - Added `--symbols` and `--symbols-manifest`.
  - New runs can now use a fixed universe from an existing manifest instead of rediscovering live volume.
- Updated `launcher/launcher_server.py`:
  - Download monitor now prioritizes any active download process by parsing `--run-id`.
  - Resume logic can restart training-history runs that were seeded from `source_manifest`.

Started job:
```bash
python3 scripts/fetch_training_history.py \
  --run-id train_hist_134_5m_20240101_20260424 \
  --symbols-manifest engine/data/training_history/train_hist_vol1m_1h_20240101_20260424/manifest.json \
  --start 2024-01-01 \
  --end 2026-04-24 \
  --timeframes 5m \
  --sleep-sec 2 \
  --retry-attempts 8 \
  --retry-sleep-sec 20 \
  --min-rows 100
```

Current status at checkpoint:
- Run id: `train_hist_134_5m_20240101_20260424`
- Output path: `engine/data/training_history/train_hist_134_5m_20240101_20260424`
- Process PID: `49434`
- Tool session id in this Codex turn: `2798`
- Total jobs: `134`
- Current item: `BTC/USDT` `5m`
- Monitor URL: `http://127.0.0.1:8788/`

## 2026-04-26 — Data Download Review

Reviewed data status after user observed completion.

Completed datasets:
- 1h OHLCV/funding:
  - Run id: `train_hist_vol1m_1h_20240101_20260424`
  - Latest per-symbol jobs: 132 ok, 2 failed.
  - Failed due short listed history under `min_rows=100`:
    - `OPG/USDT`: 30 rows
    - `CHIP/USDT`: 73 rows
  - These are not missing from the 5m run.
- 5m OHLCV/funding:
  - Run id: `train_hist_134_5m_20240101_20260424`
  - Manifest status: completed.
  - Latest jobs: 134/134 ok.
  - Cache files: 134 matching `*_futures_5m.*`.
  - All file interval modes: `5m`.
  - Row counts: min 600, max 243600.
  - Actual earliest timestamp range: `2024-01-01 00:00 UTC` to `2026-04-23 09:00 UTC` depending on contract listing time.
  - Actual latest timestamp range: `2026-04-25 00:00 UTC` to `2026-04-25 23:30 UTC`.
  - Note: actual cache extends beyond manifest end `2026-04-24` because date-only end handling fetches through the following day/current available bars.
- 5m derivatives structure:
  - Run id: `deriv_struct_132_5m_20240101_20260424`
  - Latest jobs: 402/402 ok.
  - Kinds: `funding`, `open_interest`, `long_short`.
  - Actual OI coverage remains short because OKX exposes only recent 5m OI through this path:
    - OI 5m rows per symbol: 500-507
    - OI coverage: about 1 day 18 hours
  - Long/short coverage:
    - 5m rows per symbol: 100
    - coverage: about 8h15m
  - Funding uses funding settlement cadence, not true 5m.

Current data storage:
- `engine/data/cache`: about `564M`
- `engine/data/training_history`: about `136K` manifests/progress only; actual OHLCV cache lives in `engine/data/cache`
- `engine/data/derivatives_structure`: about `5.8M`
- `engine/data/microstructure`: about `684K`
- `engine/data/features`: about `5.0M` smoke feature datasets only

Conclusion:
- Broad 5m OHLCV for the 134-symbol universe is complete enough to build the first serious 5m feature dataset.
- Derivatives structure is complete for what OKX/ccxt exposed, but OI and long/short are short recent windows, not long historical panels.
- The next missing layer is not more raw OHLCV; it is materializing a production feature dataset from 5m OHLCV plus available derivatives structure and then running validation/selection.

## 2026-04-26 — OI Historical Depth Clarification

User challenged whether 1-2 days of OI is useful.

Direct OKX native REST tests:
- Endpoint tested: `/api/v5/rubik/stat/contracts/open-interest-volume`
- Currency: `BTC`
- Findings:
  - `period=5m` without begin/end returns 576 rows, covering about 48 hours.
  - `period=1H` without begin/end returns 720 rows, covering about 30 days.
  - `period=1D` without begin/end returns 180 rows, covering about 180 days.
  - Attempts to request 5m windows older than the recent window with `end=` returned `50030 Illegal time range`.
  - Attempts to request 1H older than the recent 30-day window returned `50030 Illegal time range`.
  - 1D allows recent windows inside the 180-day span but not older history.

Conclusion:
- 5m OI is not suitable for historical model training or walk-forward backtests.
- 5m OI can only be used for:
  - current market state,
  - very recent live feature monitoring,
  - short-term sanity checks around active strategies.
- For training:
  - use 5m OHLCV/funding as the main broad panel,
  - use OI only as lower-frequency regime feature if fetching 1H/1D panels,
  - or find an external historical derivatives data source if long-history OI is required.

Recommended adjustment:
- Do not include 5m OI as a historical feature in the first production training dataset except for the final recent tail.
- Optionally fetch:
  - 1H OI for about 30 days across the universe,
  - 1D OI for about 180 days across the universe,
  and align as low-frequency regime/crowding features.

## 2026-04-26 — Monster Coin Research Started

User objective:
- Shift focus from generic quant features to a "妖币捕捉" strategy.
- Study coins that rise 40-50% in a day, multiple times in 3-10 days, or extreme examples such as many-fold moves.
- Identify pre-explosion features and collect necessary data.

Initial diagnosis:
- Long-history 5m OI is not usable for this goal because OKX exposes only a short recent window.
- Main historical research panel should be 134-symbol 5m OHLCV/funding.
- Monster strategy must avoid confusing market-wide crash rebound events with idiosyncratic monster coins.
  - Example from first scan: many symbols show extreme return from `2025-10-10 21:15 UTC`, likely a broad market dislocation/rebound cluster.

New data fetched:
- Metadata snapshot:
  - Dataset id: `monster_universe_metadata_20260426`
  - Path: `engine/data/microstructure/monster_universe_metadata_20260426`
  - Kinds: `ticker`, `instrument`
  - Symbols: 134
  - Result: all ticker/instrument artifacts succeeded.
  - Use: listing time, contract size, tick/amount precision, leverage/specs, current liquidity snapshot.

New research script:
- Added `scripts/mine_monster_events.py`.
- Mines explosive return events from cached 5m OHLCV.
- Labels:
  - 1d return >= 40%
  - 3d return >= 100%
  - 5d/10d return >= 200%
  - event de-duplication gap: 24 hours
- Pre-event features:
  - age_days
  - pre_ret_1h / 6h / 24h / 3d
  - realized vol 6h / 24h
  - volume vs 7d baseline and volume z-score
  - median market 1d return
  - idiosyncratic 1d return
  - cross-sectional rank over 6h and 1d

Monster event dataset:
- Dataset id: `monster_events_5m_v1`
- Path: `engine/data/monster_events/monster_events_5m_v1`
- Artifact: `events.parquet`
- Events mined: `580`
- By horizon:
  - `1d`: 342
  - `3d`: 145
  - `5d`: 19
  - `10d`: 74
- Top symbols by event count include:
  - `SOON/USDT`, `RAVE/USDT`, `PIPPIN/USDT`, `CORE/USDT`, `FARTCOIN/USDT`, `SPK/USDT`, `BEAT/USDT`, `BIO/USDT`, `RIVER/USDT`, `GRASS/USDT`.
- Notable mined examples:
  - `RAVE/USDT` 10d future return about `98x`, event start `2026-04-08 06:30 UTC`.
  - `RAVE/USDT` 5d future return about `43x`, event start `2026-04-09 11:55 UTC`.
  - `LIGHT/USDT` 1d future return about `5.9x`, event start `2025-12-31 09:25 UTC`.
  - `CORE/USDT` 10d future return about `6.2x`, event start `2024-03-22 22:30 UTC`.

Next research actions:
- Build negative samples and a point-in-time training table.
- Filter or separately tag market-wide rebound clusters using market median return and cross-sectional breadth.
- Add "pre-breakout compression" features: low volatility, range squeeze, low volume followed by volume expansion, proximity to local high, age/listing bucket.
- For live trading, collect continuous recent trades/orderbook snapshots for candidate watchlist only, not all 134 initially.

## 2026-04-26 — Monster Coin Research Plan v1

Current objective:
- Turn raw monster-event mining into a usable research/training pipeline for a "妖币捕捉" strategy.

Immediate steps:
1. Build event/negative sample dataset.
   - Positive labels: explosive future return events already mined in `monster_events_5m_v1`.
   - Negative labels: sampled symbol-times not followed by explosive moves.
   - Must be point-in-time: all features use data before timestamp only.
2. Add richer pre-breakout features:
   - return/momentum ladder,
   - volatility compression,
   - range compression,
   - volume dry-up and volume expansion,
   - distance to local high / breakout proximity,
   - cross-sectional relative strength,
   - market-wide rebound filters,
   - listing age.
3. Run feature diagnostics:
   - positive vs negative distribution,
   - simple univariate separation,
   - identify features that are likely leak-prone or not robust.
4. Define first watchlist scorer:
   - no trading yet,
   - output top candidates and reasons.
5. Identify missing data:
   - what can be filled from current cache,
   - what needs new live/recent collectors.

Status:
- About to implement step 1 and 2 as `scripts/build_monster_dataset.py`.

## 2026-04-26 10:45 CST — Monster Sample Dataset v1

Completed:
- Added `scripts/build_monster_dataset.py`.
- Built first point-in-time monster-coin sample table:
  - Dataset: `engine/data/monster_events/monster_samples_5m_v1/`
  - Samples: `engine/data/monster_events/monster_samples_5m_v1/samples.parquet`
  - Feature diagnostics: `engine/data/monster_events/monster_samples_5m_v1/feature_summary.csv`
  - Manifest: `engine/data/monster_events/monster_samples_5m_v1/manifest.json`
- Rows: 3078 total = 513 positive + 2565 negative.
- Features: 86 point-in-time features.
- Loaded symbols: 132 of 134. Missing/too short for this dataset: `CHIP/USDT`, `OPG/USDT`.

First findings:
- Strongest current separators are pre-event realized volatility and range expansion:
  - `rvol_6h` AUC 0.817
  - `rvol_12h` AUC 0.814
  - `rvol_24h` AUC 0.812
  - `range_pct_6h` AUC 0.811
  - `range_pct_3h` AUC 0.807
- Volume features also separate positives, but weaker than volatility/range:
  - `volume_mean_15m` AUC 0.757
  - `volume_sum_1h` AUC 0.748
- This suggests the first practical monster strategy should be an early-abnormal-activity continuation detector, not a pure quiet-accumulation detector.
- Market-wide event contamination exists but is now tagged:
  - positive samples with `market_event_flag=1`: 18.5%
  - negative samples with `market_event_flag=1`: 1.2%

Data gaps / next data needs:
- OKX historical 5m OI is only useful for the most recent ~48 hours, so it cannot train 2024-2026 monster labels directly.
- Need live/recent collection for orderbook, trades, OI, and long/short on candidate watchlists, not necessarily all 132 symbols.
- Need a model/scorer layer that can use only live-available fields first: OHLCV range/vol/volume/cross-sectional ranks/market event flags.
- Later external data likely matters for true early capture: listing age/source, announcement/social/news/search, exchange listing events, and token unlock/news shocks.

Verification:
- `PYTHONPYCACHEPREFIX=/tmp/okx_pycache python3 -m py_compile scripts/build_monster_dataset.py` passed.
- Logged and resolved `PYCOMPILE_CACHE_PERMISSION` because plain `python3 -m py_compile` attempted to write pyc under `~/Library/Caches`, which is blocked by sandbox permissions.

Next immediate implementation:
- Build `scripts/score_monster_watchlist.py` using the first diagnostic features.
- Produce a current watchlist from the latest 5m bars with scores, trigger reasons, and market-event filtering.
- Then add a simple event-study report around top historical monsters to distinguish pre-pump, first-leg, consolidation, and continuation phases.

## 2026-04-26 10:49 CST — Monster Watchlist Scorer v1

Completed:
- Added `scripts/score_monster_watchlist.py`.
- Patched `scripts/build_monster_dataset.py` so `_sample_row(..., require_forward=False)` can score latest bars without needing future data.
- Built latest cached watchlist:
  - Dataset: `engine/data/monster_events/monster_watchlist_5m_latest_v1/`
  - CSV: `engine/data/monster_events/monster_watchlist_5m_latest_v1/watchlist.csv`
  - Parquet: `engine/data/monster_events/monster_watchlist_5m_latest_v1/watchlist.parquet`
  - Top JSON: `engine/data/monster_events/monster_watchlist_5m_latest_v1/watchlist_top.json`
  - Manifest: `engine/data/monster_events/monster_watchlist_5m_latest_v1/manifest.json`
- Symbols scored: 132.
- Verification: `PYTHONPYCACHEPREFIX=/tmp/okx_pycache python3 -m py_compile scripts/build_monster_dataset.py scripts/score_monster_watchlist.py` passed.

Top cached candidates:
- `KAT/USDT` score 0.957
- `ZBT/USDT` score 0.952
- `BSB/USDT` score 0.945
- `RAVE/USDT` score 0.919
- `APE/USDT` score 0.906
- `LAB/USDT` score 0.901
- `CORE/USDT` score 0.888
- `API3/USDT` score 0.856
- `AXS/USDT` score 0.849
- `PIPPIN/USDT` score 0.819

Important caveat:
- This watchlist is based on the latest local cached bars, not real-time OKX data.
- Latest sample timestamps vary by symbol and are mostly on `2026-04-25`, so this is a research artifact only.
- Before using it for live trading, add a fresh-data gate and a real-time fetch/refresh step.

Next immediate implementation:
- Add staleness fields/gates to the watchlist scorer.
- Build an event-study report around historical top monster events to inspect the pre-pump / first-leg / pullback / continuation structure.
- Then convert the scorer into a candidate generator with explicit trade filters: liquidity, spread, freshness, market-event filter, max drawdown from local high, and stop placement rules.

## 2026-04-26 10:51 CST — Watchlist Freshness Gate

Completed:
- Added `bar_age_hours` and `fresh_data_flag` to `scripts/score_monster_watchlist.py`.
- Rebuilt `engine/data/monster_events/monster_watchlist_5m_latest_v1/`.

Current status:
- All top cached candidates have `fresh_data_flag=0`.
- This confirms the current watchlist is stale research output, not actionable live output.
- Freshness threshold is currently 1 hour.

Next:
- Add a refresh command that fetches the latest 5m bars before scoring.
- Keep the scorer separate from trading until freshness, liquidity, and risk gates pass.

## 2026-04-26 10:53 CST — Monster Event Study v1

Completed:
- Added `scripts/build_monster_event_study.py`.
- Built event-study dataset:
  - Dataset: `engine/data/monster_events/monster_event_study_5m_v1/`
  - Event study CSV: `engine/data/monster_events/monster_event_study_5m_v1/event_study.csv`
  - Phase summary CSV: `engine/data/monster_events/monster_event_study_5m_v1/phase_summary.csv`
  - Markdown report: `engine/data/monster_events/monster_event_study_5m_v1/report.md`
  - Manifest: `engine/data/monster_events/monster_event_study_5m_v1/manifest.json`
- Rows studied: 106 of top 120 events. Missing rows are mostly too close to data boundaries or missing loaded symbols.
- Verification: `PYTHONPYCACHEPREFIX=/tmp/okx_pycache python3 -m py_compile scripts/build_monster_event_study.py` passed.

Phase summary, top extreme events:
- Median pre-event returns are slightly negative:
  - 7d: -6.0%
  - 3d: -3.2%
  - 24h: -1.4%
  - 6h: -1.6%
  - 1h: -0.9%
- Median post-event returns are positive:
  - 1h: +0.95%
  - 6h: +2.54%
  - 24h: +9.67%
  - 3d: +49.9%
- Median max return after signal:
  - 24h max: +12.8%
  - 3d max: +59.4%
- Median pullback after first 24h high: -9.6%.

Interpretation:
- For the largest historical monsters, the timestamp selected by the label often appears near the end of a weak/flat pre-period and before a multi-day continuation.
- A practical strategy should probably avoid buying the most vertical first spike blindly; better structure is:
  1. detect abnormal volatility/range/volume,
  2. require fresh data and liquidity/spread pass,
  3. wait for continuation or controlled pullback after the first impulse,
  4. use hard stops because median pullback after first 24h high is close to -10%.
- The top event study is dominated by repeated timestamps from the same monster run, especially `RAVE/USDT`. Next training version should de-duplicate by symbol and event cluster so one coin does not dominate the model.

Next:
- Build clustered event labels: one monster episode per symbol per 3-10 day run.
- Rebuild samples and feature diagnostics on clustered labels.
- Add fresh 5m refresh before scoring so watchlist can become operational.

## 2026-04-26 10:58 CST — Clustered Monster Labels v1

Completed:
- Added `scripts/cluster_monster_events.py`.
- Clustered repeated event labels into symbol-level monster episodes:
  - Dataset: `engine/data/monster_events/monster_episodes_5m_v1/`
  - Artifact: `engine/data/monster_events/monster_episodes_5m_v1/episodes.parquet`
  - Source events: 580
  - Clustered episodes: 319
  - Cluster gap: 10 days
- Built clustered sample dataset:
  - Dataset: `engine/data/monster_events/monster_samples_clustered_5m_v1/`
  - Rows: 1716 total = 286 positive + 1430 negative
  - Features: 86
- Built clustered watchlist:
  - Dataset: `engine/data/monster_events/monster_watchlist_5m_clustered_v1/`
  - CSV: `engine/data/monster_events/monster_watchlist_5m_clustered_v1/watchlist.csv`
  - Parquet: `engine/data/monster_events/monster_watchlist_5m_clustered_v1/watchlist.parquet`
  - Top JSON: `engine/data/monster_events/monster_watchlist_5m_clustered_v1/watchlist_top.json`

Clustered feature findings:
- Strong features remain stable after de-duplication:
  - `dist_high_6h` AUC 0.228, lower is more monster-like
  - `rvol_6h` AUC 0.771
  - `range_pct_6h` AUC 0.770
  - `range_pct_15m` AUC 0.770
  - `range_pct_3h` AUC 0.769
  - `rvol_12h` AUC 0.768
- Interpretation: independent monster episodes often start from a violent volatility/range expansion while price is not sitting at the local high. This favors a "violent range + pullback/continuation" candidate generator over pure breakout-at-high chasing.

Top clustered cached watchlist:
- `ZBT/USDT` score 0.938
- `KAT/USDT` score 0.933
- `RAVE/USDT` score 0.910
- `BSB/USDT` score 0.904
- `APE/USDT` score 0.891
- `CORE/USDT` score 0.879
- `LAB/USDT` score 0.852
- `API3/USDT` score 0.826
- `AXS/USDT` score 0.822
- `TRUMP/USDT` score 0.790

Important caveat:
- Clustered watchlist is still based on stale local cached bars; all top rows have `fresh_data_flag=0`.

Next:
- Add a fresh-data refresh step before scoring.
- Add liquidity/spread checks from ticker/orderbook metadata.
- Build first candidate generator rules:
  - fresh bar <= 10 minutes,
  - score high under clustered model,
  - not market-wide rebound,
  - adequate quote volume/spread,
  - avoid buying after >20-30% 1h vertical move,
  - prefer controlled pullback while 6h/24h abnormal range remains high.

## 2026-04-26 11:21 CST — Live-Gated Monster Scan v1

Completed:
- Added `scripts/refresh_monster_latest.py`.
  - Refreshes recent OKX public 5m OHLCV.
  - Safely merges recent data into existing cache with concat + sort + duplicate timestamp keep-last.
  - Fetches ticker + orderbook snapshot.
  - Writes `status.json`, `progress.jsonl`, `market_snapshot.csv/parquet`, and `manifest.json`.
- Extended `scripts/score_monster_watchlist.py`:
  - Optional `--market-snapshot`.
  - Adds `liquidity_gate` and `trade_candidate_flag`.
  - Gate defaults:
    - fresh bar <= 0.25 hours
    - quote volume >= 1,000,000 USDT
    - spread <= 50 bps
    - orderbook depth within 1% >= 5,000 USDT
    - score >= 0.75
    - 1h return <= +25%
    - not market-wide rebound flag
- Fixed and logged `PANDAS_5M_FREQ_REFRESH`: pandas interpreted `5m` as month-end for `ceil()`. Script now maps `5m -> 5min`.

Refresh runs:
- Smoke refresh:
  - Dataset: `engine/data/monster_events/monster_latest_refresh_smoke_20260426/`
  - Symbols: ZBT/KAT/RAVE/BSB/APE
  - Result: 5/5 ok
- Core control refresh:
  - Dataset: `engine/data/monster_events/monster_latest_refresh_core_smoke_20260426/`
  - BTC/ETH refreshed to `2026-04-26 03:15 UTC`
- Full refresh:
  - Dataset: `engine/data/monster_events/monster_latest_refresh_134_20260426/`
  - Result: 134/134 ok
  - Market snapshot: `engine/data/monster_events/monster_latest_refresh_134_20260426/market_snapshot.parquet`

Live-gated scan:
- Dataset: `engine/data/monster_events/monster_watchlist_5m_live_gated_20260426/`
- Watchlist CSV: `engine/data/monster_events/monster_watchlist_5m_live_gated_20260426/watchlist.csv`
- Rows scored: 132
- Fresh rows: 132
- Liquidity-pass rows: 99
- Trade candidates under current gates: 6

Current live-gated candidates:
- `AXS/USDT`: score 0.898, 1h -1.57%, 6h -6.92%, 24h +19.40%, quote vol ~4.36B, spread 0.70 bps, depth ~738k.
- `CORE/USDT`: score 0.874, 1h -0.72%, 6h +0.22%, 24h -6.04%, quote vol ~17.0M, spread 2.42 bps, depth ~62k.
- `APE/USDT`: score 0.871, 1h +0.04%, 6h -4.21%, 24h -26.60%, quote vol ~1.07B, spread 0.66 bps, depth ~338k.
- `PIPPIN/USDT`: score 0.840, 1h -1.51%, 6h -8.14%, 24h +11.69%, quote vol ~6.17M, spread 3.34 bps, depth ~10k.
- `API3/USDT`: score 0.783, 1h -0.35%, 6h -6.72%, 24h -27.67%, quote vol ~75.6M, spread 2.92 bps, depth ~140k.
- `SOON/USDT`: score 0.770, 1h +1.61%, 6h -4.77%, 24h -22.21%, quote vol ~3.36M, spread 5.28 bps, depth ~12k.

Interpretation:
- The highest raw scores BSB/KAT/ZBT/RAVE/LAB failed liquidity gate, mostly because 1% orderbook depth is below 5,000 USDT.
- The actual trade candidates are not necessarily the highest raw scores; they are the first rows that pass freshness + liquidity + score + market filters.
- No orders placed. This remains candidate scanning only.

Next:
- Build a paper-trade/backtest harness for the live-gated candidate generator:
  - enter at next 5m open/close after signal,
  - test 1h/6h/24h holds plus stop/take-profit,
  - include fees/slippage,
  - compare against random candidate times and against raw high-score-only mode.
- Add candidate export to the launcher page so the UI can show scan status and current live-gated candidates.

## 2026-04-26 11:24 CST — Candidate Rule Calibration v1

Completed:
- Added `scripts/evaluate_monster_candidate_rules.py`.
- Evaluated clustered scorer on `monster_samples_clustered_5m_v1` with known forward returns.
- Dataset: `engine/data/monster_events/monster_rule_eval_clustered_v1/`
  - Report: `engine/data/monster_events/monster_rule_eval_clustered_v1/report.md`
  - Threshold summary: `engine/data/monster_events/monster_rule_eval_clustered_v1/threshold_summary.csv`
  - Scored samples: `engine/data/monster_events/monster_rule_eval_clustered_v1/scored_samples.parquet`

Calibration result:
- At score >= 0.75:
  - rows 191
  - positive rate 28.8%
  - 1d median return after 8 bps cost: +2.30%
  - 3d median: +3.09%
  - 5d median: +5.08%
  - 1d win rate: 60.7%
  - median 5d max favorable excursion: +24.1%
  - median 5d adverse excursion: -10.7%
- At score >= 0.90:
  - rows 24
  - positive rate 58.3%
  - 1d median: +12.8%
  - 3d median: +23.6%
  - 5d median: +22.4%
  - 1d win rate: 79.2%
  - median 5d max favorable excursion: +58.4%
  - median 5d adverse excursion: -6.5%

Interpretation:
- Score threshold matters; higher score materially improves positive rate and forward-return distribution.
- But drawdown risk is still large around the 0.75 threshold; the candidate generator needs stop/entry timing, not just ranking.
- This is sample-set calibration, not a full chronological portfolio backtest. Next step is to build a real time-series paper-trade harness.

Next:
- Add `/api/monster` to launcher/dashboard so current live-gated candidates are visible.
- Build full chronological backtest over the cached 5m panel for top-score candidates with next-bar entry, stops, take-profit, and max concurrent positions.

## 2026-04-26 11:32 CST — Launcher Monster Scan Panel

Completed:
- Added `MONSTER_EVENTS_DIR` and `/api/monster` to `launcher/launcher_server.py`.
- Added a "妖币扫描" panel to `launcher/static/index.html`.
- Added frontend polling/rendering in `launcher/static/app.js`.
- Added compact panel/table styles in `launcher/static/style.css`.
- Restarted launcher on `http://127.0.0.1:8788/`.

Verification:
- `PYTHONPYCACHEPREFIX=/tmp/okx_pycache python3 -m py_compile launcher/launcher_server.py` passed.
- `curl -s http://127.0.0.1:8788/api/monster` returns the live-gated scan.
- `curl -s http://127.0.0.1:8788/app.js` confirms `refreshMonsterStatus()` is served.

Current UI behavior:
- Shows latest monster watchlist run id.
- Shows fresh top count, liquidity top count, and trade candidate count.
- Displays trade candidates first; falls back to top scored rows if no candidates.
- Refreshes monster scan display every 10 seconds.

Next:
- Add a button to run a fresh monster refresh + rescore from the UI.
- Build chronological paper-trade backtest before any live execution.

## 2026-04-26 11:49 CST — Monster Pipeline Completion Plan

User objective:
- Finish the missing parts of the full quant pipeline for the monster-coin strategy.
- Persist progress continuously so another Codex session can resume safely.

Current architecture status:
- Data: mostly complete for OHLCV and live ticker/orderbook snapshots.
- Feature engineering: usable v1 for monster OHLCV/range/vol/volume/cross-sectional features.
- Feature selection: univariate and clustered-label diagnostics exist, but walk-forward stability is still missing.
- Candidate generation: live-gated scorer exists and is shown in launcher.
- Strategy: not complete yet; candidate generator lacks formal entry/exit/position/risk rules.
- Backtest: generic framework exists, but monster strategy lacks strict chronological portfolio backtest.
- Evaluation: sample calibration exists, but no full equity curve / drawdown / trade ledger for monster.
- Paper trading: not implemented.
- Production risk monitoring: partial existing engine risk; monster-specific risk not implemented.

Remaining work plan:
1. Build chronological monster backtest.
   - Use cached 5m OHLCV.
   - Compute point-in-time features over time.
   - Score each symbol at decision bars.
   - Enter next bar after signal.
   - Support max positions, capital per trade, fees/slippage, stop loss, take profit, trailing/time exit.
   - Output trade ledger, equity curve, metrics, and manifest.
2. Formalize monster strategy spec.
   - Entry filters: fresh data, score threshold, market-event filter, liquidity gate, no vertical 1h chase.
   - Exit rules: stop, take profit, trailing stop, time stop.
   - Position rules: max concurrent names, max notional, cooldown.
3. Build walk-forward/stability evaluation.
   - Split by time.
   - Report fold-level returns, win rate, drawdown, and candidate counts.
4. Add paper-trading loop.
   - Refresh latest data.
   - Score candidates.
   - Record hypothetical entries/exits only; no orders.
   - Persist decisions and future outcome labels.
5. Add launcher controls.
   - Button/API to run fresh monster refresh + rescore.
   - Show backtest summary and paper-trade status.
6. Add risk monitor for monster.
   - Stale data, spread/depth, slippage, drawdown, max concurrent positions, repeated-loss circuit breaker.

Immediate next task:
- Implement `scripts/backtest_monster_strategy.py` as the first complete chronological portfolio test.
## 2026-04-26 11:54 CST — Implementing Chronological Monster Backtest

- Current task: add the missing strict chronological backtest layer for the monster-coin strategy.
- Scope: new `scripts/backtest_monster_strategy.py` using the same point-in-time feature builder and score function as the live watchlist, with 5m bar-by-bar position accounting and hourly signal decisions by default.
- Safety: no order placement; all outputs go under `engine/data/monster_events/<dataset-id>/` so interrupted runs do not delete or overwrite unrelated historical data.

## 2026-04-26 12:07 CST — Monster Pipeline Patches Added

- Added `scripts/backtest_monster_strategy.py`: chronological 5m backtest, same score features as watchlist, conservative stop-first intrabar handling, outputs trades/equity/signals/metrics/manifest.
- Added `scripts/run_monster_refresh_and_score.py`: launcher-friendly wrapper to refresh latest public OKX data then rebuild the monster watchlist; no orders.
- Added `scripts/run_monster_paper.py`: paper-only loop that reads latest watchlist, simulates entries/exits, writes `engine/logs/monster_paper/*.json` and ledger JSONL; no orders.
- Added `.claude/knowledge/strategies/monster_coin.md` and indexed it in `.claude/knowledge/strategies/_index.md`.
- Launcher updated with `/api/monster-refresh` and a "刷新并重扫" button in the monster panel.
- Verification so far: Python compile passed for new scripts and launcher; `run_monster_paper.py --state-id smoke_20260426` opened simulated paper positions in AXS/CORE/APE using latest watchlist only.
- Current running job: `scripts/backtest_monster_strategy.py --dataset-id monster_backtest_5m_v1 --start 2025-01-01 --end 2026-04-26 --rebalance-minutes 240`.

## 2026-04-26 21:20 CST — Long Monster Backtest Completed

- Completed `monster_backtest_5m_v1` over 2025-01-01 to 2026-04-26.
- Result: final NAV 545.75 from 1000.00, total return -45.43%, max drawdown -73.62%, 1229 trades, win rate 34.17%, profit factor 0.93.
- Interpretation: the raw monster score is not production-ready. It catches some explosive moves, but the default threshold/holding/risk rules overtrade and decay badly outside short favorable windows.
- Next required work: add diagnostics by month/symbol/score bucket/exit reason, then run parameter sweeps and walk-forward checks before any live execution.

## 2026-04-26 21:23 CST — Monster Article Research Note

- User shared WeChat article `https://mp.weixin.qq.com/s/zLjtZZte8dagslU6fC5JkQ`; direct access failed from browser tool, but the same article appears mirrored by Bitget/Odaily: `https://www.bitget.com/zh-CN/news/detail/12560605364218`.
- Key model implications: monster coins are more about controlled float/market-maker behavior than normal momentum; OI/volume can be deliberately manipulated; price up + OI down on 1h is a crash-risk signal; liquidation clusters + sharp OI drop can mark maker exit.
- Pipeline change needed: add derivatives/OI divergence, cross-exchange OI share anomaly, volume/OI turnover anomaly, liquidation proxy if available, and stricter "exit before collapse" logic. Current OHLCV-only score is insufficient.

## 2026-04-26 21:57 CST — Monster Lottery Perp Direction Approved

- User clarified the intended strategy: find a monster coin, risk only 10-20 USDT initial capital per attempt, roll winners aggressively, and accept low win rate because one large winner can pay for many failed attempts.
- Created full dated plan: `.claude/MONSTER_LOTTERY_PLAN_2026-04-26.md`.
- Architecture shift: ordinary continuous NAV-allocation backtest is not the correct objective. Need fixed-risk, convex-payoff, long/short lottery-style strategy.
- Immediate build queue:
  1. Diagnose `monster_backtest_5m_v1` losses.
  2. Implement candidate-only orderbook collector.
  3. Implement lottery backtest with risk budget, leverage approximation, staged exits, and long/short support.
  4. Extend paper loop and launcher only after the data collector/backtest are smoke-tested.

## 2026-04-26 22:00 CST — Monster Backtest Diagnostics

- Ran `python3 scripts/analyze_monster_backtest.py --backtest-id monster_backtest_5m_v1`.
- Outputs written under `engine/data/monster_events/monster_backtest_5m_v1/analysis_*.csv` and `analysis_summary.json`.
- Key finding: not a lack of big winners. Take-profit trades: 155 trades, +4351.40 PnL. Time exits: 125 trades, +681.49 PnL.
- Main drag: stop/trailing exits: 948 trades, -5496.39 PnL, 18.6% win rate, median return -7.06%.
- Best symbols include FARTCOIN, BEAT, MERL, EIGEN, TRB, CORE, KAT. Worst symbols include PI, GRASS, API3, SPK, IP.
- This supports the Monster Lottery redesign: fixed small loss per attempt, fewer low-quality repeats, and allow right-tail winners to run under protected rolling rules.

## 2026-04-26 22:09 CST — Candidate Orderbook Collector Added

- Added `scripts/collect_monster_orderbook.py`.
- Purpose: collect append-only OKX orderbook snapshots for only current monster watchlist candidates; no orders.
- Outputs: `orderbook_snapshots.jsonl/csv/parquet`, `orderbook_features.jsonl/csv/parquet`, `progress.jsonl`, `status.json`, `manifest.json`.
- Smoke test v1: `monster_orderbook_smoke_20260426`, 5 symbols OK, but depth field naming was unclear.
- Fixed field naming to `depth_1pct_usd`, `depth_50bps_usd`, `depth_2pct_usd`.
- Smoke test v2: `monster_orderbook_smoke_20260426_v2`, AXS/CORE/APE OK with 1% depth populated.
- Network note: sandbox DNS blocked OKX, reran with approved network escalation for `python3 scripts/collect_monster_orderbook.py`.

## 2026-04-26 22:20 CST — Monster Lottery Backtest V1 Smoke

- Added `scripts/backtest_monster_lottery.py`.
- Supports fixed `risk_budget`, leverage approximation, long/short signals, hard stop, TP1/TP2 partial exits, runner trailing stop, max hold, cooldown, and metrics by risk budget.
- Smoke command: `python3 scripts/backtest_monster_lottery.py --dataset-id monster_lottery_smoke_20260426 --start 2026-01-01 --end 2026-04-26 --rebalance-minutes 240 --risk-budget 20 --long-score 0.90`.
- Result: final NAV 990.54 from 1000.00, total return -0.95%, max drawdown -36.54%, 223 trade events, event win rate 41.26%, profit factor 0.998.
- Best event: BASED/USDT runner stop, +4.78x risk budget. Average loss event about -9.35 USDT, average win event about +13.28 USDT.
- Interpretation: fixed-risk lottery framing already compresses the large 2026 drawdown from the continuous allocation model, but default parameters still need sweeping and better live-only orderbook/OI filters.

## 2026-04-26 22:24 CST — Launcher Orderbook Controls Added

- Launcher backend:
  - Added `latest_monster_orderbook_id()`.
  - Added `monster_orderbook_status()`.
  - Added `start_monster_orderbook()`.
  - Added GET `/api/monster-orderbook`.
  - Added POST `/api/monster-orderbook-start`.
- Launcher frontend:
  - Added "采盘口" button in monster panel.
  - Added `monsterOrderbookStatus` row with run id, ok/failed counts, last symbol, and 1% depth when available.
- Verification: `PYTHONPYCACHEPREFIX=/tmp/okx_pycache python3 -m py_compile launcher/launcher_server.py scripts/backtest_monster_lottery.py scripts/collect_monster_orderbook.py` passed.
- Note: running launcher must be restarted to pick up this UI/backend patch.

## 2026-04-26 22:45 CST — Next Execution Queue

User approved the next queue. Execute in order:

1. Lottery parameter sweep:
   - risk budget: 10/20.
   - long score: 0.88/0.90/0.92/0.95.
   - stop loss: 8%/10%/15%.
   - TP/trailing combinations.
   - Objective: payoff skew, bounded loss, survival, max drawdown, and right-tail winners, not raw win rate.
2. Candidate-only OI/funding collector:
   - Similar to orderbook collector.
   - Read latest monster watchlist.
   - Store OI, funding, long/short if available.
   - Use for live/paper filters because long historical OI is not available.
3. Extend paper loop with lottery mode:
   - Fixed 10/20 USDT risk budget.
   - Long/short direction.
   - Staged exits and runner protection.
   - Still no real order placement.
4. Restart/verify launcher:
   - Confirm "采盘口" button and monster orderbook status render correctly.
   - Confirm API routes work.

## 2026-04-26 22:55 CST — Sweep Smoke Too Slow

- Added `scripts/sweep_monster_lottery.py` and started `monster_lottery_sweep_smoke_20260426` with 8 runs.
- Problem: the first implementation launches `backtest_monster_lottery.py` as a new subprocess for each parameter set, causing every run to reload 132 parquet files and recompute rolling features.
- Run 001 completed but was slow: risk=10, long_score=0.88, stop=0.08, TP 0.30/0.80, trailing 0.25; final NAV 828.38, total return -17.16%, max drawdown -28.67%, profit factor 0.78.
- Stopped the slow sweep after run 001 and before completing run 002.
- Next fix: replace with in-process fast sweep that loads data once and calls the lottery backtest runner for each config.

## 2026-04-26 23:00 CST — Fast Sweep Still Bottlenecked

- Added `scripts/sweep_monster_lottery_fast.py`, which avoids subprocess-per-parameter.
- Started `monster_lottery_fast_sweep_smoke_20260426`, but it still spent too long before producing first result.
- Conclusion: the dominant cost is not subprocess overhead; it is building/scoring point-in-time features from 132 symbols every time.
- Stopped the fast sweep before results.
- Next architectural fix: build a reusable historical monster signal table once, then sweep lottery execution parameters against that table. This separates expensive feature/scoring from cheap execution/risk simulation.

## 2026-04-27 00:18 CST — Signal Table Sweep Completed

- Added `scripts/build_monster_signal_table.py`.
- Built smoke signal table: `monster_signal_table_smoke_20260426`, 2026-04-01 to 2026-04-26, 151 decision points, 1749 candidate signals.
- Built main signal table: `monster_signal_table_2026q1q2_20260426`, 2026-01-01 to 2026-04-26, 691 decision points, 6175 candidate signals.
- Added `scripts/sweep_monster_lottery_from_signals.py`.
- Fixed pandas compatibility issue: current `DatetimeIndex` lacks `union_many`, replaced with iterative `union`.
- Ran `monster_lottery_signal_sweep_2026q1q2_20260426`, 48 configs.
- Best config:
  - risk_budget=20 USDT
  - long_score=0.95
  - stop_loss=0.15
  - tp1=0.50
  - tp2=1.50
  - runner_trailing=0.30
  - final NAV 1211.65 from 1000.00
  - total return +21.16%
  - max drawdown -9.94%
  - trade events 24
  - win rate 50.0%
  - profit factor 2.59
  - best return on risk budget 4.48x
  - max consecutive losing events 4
- Interpretation: the lottery structure starts working when it is selective (`score>=0.95`) and gives winners large room. Low score thresholds (0.88/0.90) overtrade and lose badly, especially with 20 USDT risk.

## 2026-04-27 09:38 CST — Resume Checkpoint

- User asked to continue and requested current status plus next steps.
- Engine status check: `python3 engine/main.py status` reports stale snapshot only; old pid `73327` is not running.
- Current completed research state:
  - Monster event mining, dataset, event study, latest scoring, signal table, and signal-table lottery sweep are implemented.
  - Best current historical lottery config on 2026-Q1/Q2 signal table: `risk_budget=20`, `long_score=0.95`, `stop_loss=0.15`, `tp1=0.50`, `tp2=1.50`, `runner_trailing=0.30`, `+21.16%` return, `-9.94%` max drawdown, `2.59` profit factor.
  - Candidate orderbook collector exists and has smoke-tested successfully.
- Next execution queue:
  1. Add candidate-only OI/funding/live derivatives collector.
  2. Wire derivatives collector status into launcher/API so data downloads remain visible.
  3. Extend `run_monster_paper.py` with fixed-risk lottery mode.
  4. Restart and verify launcher routes/UI.
- Safety reminder: no live order placement; current work is read-only data collection, simulation, and UI wiring.

## 2026-04-27 15:49 CST — Candidate Derivatives Collector Smoke Passed

- Added `scripts/collect_monster_derivatives.py`.
- Purpose: candidate-only live derivatives snapshots for monster strategy filters.
- Scope: reads latest monster watchlist, samples top candidates, writes append-only `derivatives_features.jsonl/csv/parquet`, `progress.jsonl`, `status.json`, and `manifest.json` under `engine/data/monster_events/<dataset-id>/`.
- Smoke run: `monster_derivatives_smoke_20260427`, `top_n=3`, `candidate_only=true`, `samples=1`.
- Smoke results:
  - `AXS/USDT`: OI value `5701479.97475`, funding `2.14789374926e-05`, long/short `1.1544172234595398`.
  - `CORE/USDT`: OI value `2555002.19562`, funding `0.0001`, long/short `2.5532879818594103`.
  - `APE/USDT`: OI value `3480567.805386`, funding `-2.0403698858e-05`, long/short `1.5799632352941175`.
- Next: wire derivatives collector into launcher API/UI, then extend monster paper loop with fixed-risk lottery mode.

## 2026-04-27 16:37 CST — Launcher Wiring And Lottery Paper Mode Added

- Launcher API/UI:
  - Added latest run detection for `monster_derivatives_*`.
  - Added `monster_derivatives_status()`.
  - Added `start_monster_derivatives()`.
  - Added GET `/api/monster-derivatives`.
  - Added POST `/api/monster-derivatives-start`.
  - Added "采结构" button and `结构采集` status line in the monster panel.
- Paper loop:
  - Extended `scripts/run_monster_paper.py` with `--mode lottery`.
  - Added fixed `--risk-budget`, `--max-open-risk`, leverage-based notional sizing, staged `tp1/tp2`, runner trailing, and paper-only long/short side fields.
  - Default remains old `simple` mode for backward compatibility.
  - Shorts require explicit `--enable-shorts`.
- Verification:
  - `PYTHONPYCACHEPREFIX=/tmp/okx_pycache python3 -m py_compile launcher/launcher_server.py scripts/collect_monster_derivatives.py scripts/run_monster_paper.py` passed.
  - Smoke paper run: `python3 scripts/run_monster_paper.py --state-id lottery_smoke_20260427 --mode lottery --initial-capital 1000 --risk-budget 20 --max-open-risk 60 --score-threshold 0.85 --max-positions 3`.
  - Smoke result: opened paper positions in `AXS/USDT`, `CORE/USDT`, `APE/USDT`; cash `940.0`, nav `1000.0`, open risk `60.0`.
- Next: restart/verify launcher routes against the running local server.

## 2026-04-27 16:58 CST — Launcher Restarted And Verified

- Restarted launcher on `http://127.0.0.1:8788/`.
- New launcher pid: `88818`.
- Verified `/api/monster-derivatives`:
  - latest run `monster_derivatives_smoke_20260427`
  - `ok=3`, `failed=0`
  - last record `APE/USDT`, OI value `3480567.805386`, funding `-2.0403698858e-05`, long/short `1.5799632352941175`
- Verified `/api/monster` includes both:
  - `orderbook`: `monster_orderbook_smoke_20260426_v2`
  - `derivatives`: `monster_derivatives_smoke_20260427`
- Current next work:
  1. Run longer candidate-only orderbook + derivatives collectors if desired.
  2. Build live gating logic that consumes orderbook + OI/funding/long-short snapshots.
  3. Run `run_monster_paper.py --mode lottery` in loop after live gates are added.
  4. Add paper performance panel to launcher if paper mode becomes a persistent process.

## 2026-04-27 17:45 CST — Live Gate Integration Started

- User asked to continue and persist checkpoints.
- Current goal: make orderbook + derivatives snapshots affect actual monster entries, not just UI display.
- Planned implementation:
  1. Add a reusable live gate module that reads latest `monster_orderbook_*` and `monster_derivatives_*` feature snapshots.
  2. Join latest snapshots by `symbol`.
  3. Compute pass/fail plus reasons using conservative defaults:
     - max spread
     - min 1% book depth
     - min OI value
     - max absolute funding
     - max long/short crowding
  4. Wire the gate into paper lottery entries first, because it is safer than changing historical scoring.
  5. Run a paper smoke cycle and record results.

## 2026-04-27 18:58 CST — Live Gates And Paper Lottery Loop Running

- Added `scripts/monster_live_gates.py`.
  - Reads latest `monster_orderbook_*` and `monster_derivatives_*` snapshots.
  - Joins latest rows by `symbol`.
  - Computes `live_gate_flag`, `live_gate_reasons`, and warnings.
  - Default gates:
    - `max_snapshot_age_minutes=180`
    - `max_spread_bps=20`
    - `min_depth_1pct_usd=10000`
    - `min_open_interest_value=1000000`
    - `max_abs_funding_rate=0.0015`
    - `0.25 <= long_short_ratio <= 4.0`
- Wired live gate into `scripts/run_monster_paper.py`.
  - New flag: `--use-live-gates`.
  - Rejected candidates are logged as `live_gate_reject` in the paper ledger.
  - Accepted paper entries store live spread, depth, OI, funding, long/short, and source run ids.
- Smoke tests:
  - With stale smoke snapshots and default 180-minute age gate: no entries; rejections logged as `orderbook_stale>180m`.
  - With fresh snapshots and default age gate: 5 of 6 candidates passed; `PIPPIN/USDT` rejected for `long_short>4`.
  - Paper lottery opened `AXS/USDT`, `CORE/USDT`, `APE/USDT` with `20U` risk budget each, total open risk `60U`.
- Added launcher paper controls:
  - GET `/api/monster-paper`
  - POST `/api/monster-paper-start`
  - `/api/monster` now includes `paper`.
  - UI monster panel now has "跑纸面" button and `纸面 lottery` status row.
- Launcher restarted on `http://127.0.0.1:8788/`.
- Running processes:
  - Launcher pid from `engine/control/launcher.pid`.
  - Paper lottery loop: pid `4963`, state id `lottery_live`, interval `300s`.
  - Continuous orderbook collector: pid `5307`, run `monster_orderbook_live_20260427_185655`, current status ok `24`, failed `0`.
  - Continuous derivatives collector: pid `5308`, run `monster_derivatives_live_20260427_185655`, current status ok `6`, failed `0`.
- Current paper state:
  - `engine/logs/monster_paper/lottery_live.json`
  - NAV `1000.0`, cash `940.0`, open risk `60.0`.
  - Positions: `AXS/USDT`, `CORE/USDT`, `APE/USDT`.
- Next work:
  1. Add automatic periodic monster refresh + rescoring so watchlist is not frozen at `monster_watchlist_5m_live_gated_20260426`.
  2. Add launcher stop controls for paper/orderbook/derivatives collectors.
  3. Add paper PnL history/equity curve so the launcher can show performance over time.
  4. Promote live gate outputs into a feature-store table for later model training.

## 2026-04-27 19:06 CST — Auto Watchlist Refresh Started

- User approved automatic monster watchlist refresh and requested persistent timestamped records.
- Additional requirement: maintain a dedicated feature-combination experiment log containing:
  - tested feature combinations,
  - exact construction,
  - performance results,
  - candidate combinations still worth testing.
- Current implementation target:
  1. Create `.claude/MONSTER_FEATURE_EXPERIMENT_LOG.md`.
  2. Inspect existing `scripts/run_monster_refresh_and_score.py`.
  3. Add an automatic refresh loop so the monster watchlist is periodically rebuilt.
  4. Wire refresh-loop status/start into launcher.
  5. Verify refresh + live collectors + paper loop can coexist without deleting existing data.

## 2026-04-27 21:37 CST — Auto Watchlist Refresh Running

- Added `.claude/MONSTER_FEATURE_EXPERIMENT_LOG.md`.
- Added `scripts/run_monster_auto_refresh.py`.
  - Runs `scripts/run_monster_refresh_and_score.py` periodically.
  - Writes append-only `progress.jsonl`, `status.json`, and `manifest.json` under `engine/data/monster_events/<run-id>/`.
  - Does not place orders.
- Fixed `scripts/refresh_monster_latest.py`:
  - Added 5-attempt OKX market-load retry.
  - Fixed OHLCV pagination bug. OKX was returning max `300` 5m bars per request; old code stopped when `len(bars) < 1000`, leaving cache stale at `2026-04-26 03:15 UTC`.
  - After fix, a 3-day refresh gets `864` 5m bars and advances cache to `2026-04-27 11:20 UTC`.
- Launcher updates:
  - Added GET `/api/monster-auto-refresh`.
  - Added POST `/api/monster-auto-refresh-start`.
  - `/api/monster` now includes `auto_refresh`.
  - UI monster panel now has "自动刷新" button and status row.
  - Latest watchlist detection now accepts any run directory with `watchlist.parquet`, not only `monster_watchlist_*`.
- Updated collectors:
  - `scripts/collect_monster_orderbook.py` and `scripts/collect_monster_derivatives.py` now also accept any latest `watchlist.parquet`, so they can use auto-refresh outputs.
- Verification:
  - Smoke before pagination fix completed but produced stale watchlist; all `fresh_data_flag=0`.
  - Fixed smoke `monster_auto_refresh_smoke_fixed_20260427` completed with `ok=1`, `failed=0`.
  - New watchlist: `monster_auto_fixed_watchlist_20260427T112037Z`.
  - New watchlist has `trade_candidate_count=9`; launcher now selects it.
- Running processes after switch:
  - Auto refresh loop: pid `23430`, run `monster_auto_refresh_20260427_211629`, interval `900s`, first iteration `ok`.
  - Orderbook collector: pid `23564`, run `monster_orderbook_live_20260427_211706`, watchlist `monster_auto_fixed_watchlist_20260427T112037Z`, ok `36`, failed `0` at verification.
  - Derivatives collector: pid `23568`, run `monster_derivatives_live_20260427_211706`, watchlist `monster_auto_fixed_watchlist_20260427T112037Z`, ok `9`, failed `0` at verification.
  - Paper lottery loop from earlier remains running with state `lottery_live`.
- New live gate check on the auto-refreshed candidates:
  - Passed: `APE/USDT`, `BIO/USDT`, `CORE/USDT`, `ENSO/USDT`, `MERL/USDT`.
  - Rejected:
    - `BICO/USDT`: `oi_value<1e+06; abs_funding>0.0015`.
    - `PIPPIN/USDT`: `long_short>4`.
    - `RAVE/USDT`: `depth_1pct<10000`.
    - `ZBT/USDT`: `depth_1pct<10000`.
- Next work:
  1. Add stop controls for auto-refresh/orderbook/derivatives/paper processes in launcher.
  2. Add paper PnL/equity history panel.
  3. Make paper loop optionally replace stale/low-score positions when watchlist changes.
  4. Persist live gate tables as feature-store snapshots for future feature selection.

## 2026-04-27 21:40 CST — Paper Visualization Work Started

- User approved adding a visualization page/panel for the paper loop.
- Goal for this work block:
  1. Add persistent paper equity history from `scripts/run_monster_paper.py`.
  2. Extend launcher paper API with current positions, ledger tail, equity history, and simple summary metrics.
  3. Add a front-end paper visualization panel showing NAV, cash, unrealized PnL, open risk, positions, recent events, and an equity curve.
  4. Keep all outputs append-only and do not touch real trading/order placement.
- Current running paper loop may need restart before it starts writing the new equity history format.

## 2026-04-27 21:54 CST — Paper Visualization Running

- Completed the paper-loop visualization work.
- Files updated:
  - `scripts/run_monster_paper.py`
    - Adds append-only equity history at `engine/logs/monster_paper/<state_id>_equity.jsonl`.
    - Each paper cycle now records timestamp, cash, NAV, unrealized PnL, open risk, position count, position symbols, and live-gate status.
  - `launcher/launcher_server.py`
    - `/api/monster-paper` now returns state, positions, ledger tail, equity history, realized PnL, and summary metrics.
    - Metrics include initial NAV, current NAV, total return, max drawdown, realized PnL, and equity point count.
  - `launcher/static/index.html`
    - Adds a dedicated "纸面 Lottery" panel.
  - `launcher/static/app.js`
    - Renders paper NAV/cash/unrealized/realized/open-risk/return/drawdown/gate metrics.
    - Renders open positions, recent paper events, and an equity chart.
    - Equity chart now handles a single equity point immediately.
  - `launcher/static/style.css`
    - Adds layout and styling for the paper visualization panel.
- Verification:
  - `python3 -m py_compile launcher/launcher_server.py scripts/run_monster_paper.py` passed.
  - `node --check launcher/static/app.js` passed.
  - Restarted launcher on `http://127.0.0.1:8788/`.
  - Restarted paper lottery loop with pid `29249`.
  - `/api/monster-paper` returns:
    - `running=true`
    - `equity_path=engine/logs/monster_paper/lottery_live_equity.jsonl`
    - `cash=940.0`
    - `nav=985.821207633987`
    - `unrealized_pnl=-14.178792366012999`
    - `open_risk=60.0`
    - positions: `APE/USDT`, `AXS/USDT`, `CORE/USDT`
    - `live_gates_enabled=true`
    - `live_gate_pass_count=5`
    - `equity_points=1`
- Running background processes observed through launcher API:
  - Auto refresh loop: pid `23430`, ok `2`, failed `0`.
  - Orderbook collector: pid `23564`, ok `1439`, failed `1`.
  - Derivatives collector: pid `23568`, ok `264`, failed `0`.
  - Paper lottery loop: pid `29249`, state id `lottery_live`.
- Important note:
  - Existing paper state was preserved; no historical paper files were deleted or overwritten.
  - The equity file starts from this restart because the previous paper loop did not write equity history.
- Next work:
  1. Add stop/pause/resume controls for auto-refresh/orderbook/derivatives/paper processes in launcher.
  2. Add position replacement/rebalance logic so stale/low-score paper positions can be rotated out when watchlist refreshes.
  3. Persist live gate snapshots as feature-store tables for later feature selection and model training.
  4. Add a paper performance experiment entry to `.claude/MONSTER_FEATURE_EXPERIMENT_LOG.md` after enough paper cycles accumulate.

## 2026-04-27 23:48 CST — Paper Log Tracking Started

- User requested continuous timestamped persistence plus active monitoring of paper-loop logs and strategy quality.
- Current work block:
  1. Inspect launcher/paper API state.
  2. Inspect `lottery_live` equity history and ledger tail.
  3. Inspect collector health for orderbook/derivatives/auto-refresh.
  4. Decide whether current paper strategy needs parameter or architecture adjustment.
- No live orders will be placed.
