# Architecture Review — 2026-04-24

Scope: first-pass review of the OKX trading bot as a small-capital, high-risk quantitative system.

## Current Shape

- The repo has two trading stacks:
  - A cleaner LEAN-style pipeline: `Universe -> Alpha -> PortfolioConstruction -> Risk -> Execution`, used by `BacktestRunner`.
  - Standalone competition/live loops: `elite_flow`, `yolo_momentum`, and `yolo_orchestrator`.
- Local data cache contains broad 1h OKX futures OHLCV/funding cache under `engine/data/cache`.
- OKX public market data via Agent Trade Kit is available for tickers, order book, funding, and open interest.
- Runtime status on 2026-04-24 showed stale live summary from 2026-04-13 with `yolo_orchestrator` running in summary, one `PIEVERSE-USDT-SWAP` short, NAV about 49.13 on 50 capital.

## Main Architecture Gaps

1. No first-class feature store.
   - Features are embedded inside strategy files.
   - There is no reusable feature schema, timestamp alignment contract, quality report, or feature provenance.
   - Research features and live features can drift silently.

2. No explicit label/target layer.
   - Backtests evaluate full strategies, but there is no standardized dataset of `(features at t, forward return / drawdown / breakout / liquidation outcome at t+h)`.
   - This makes feature selection hard and encourages tuning full strategy PnL directly.

3. Live and backtest parity is split.
   - `BacktestRunner` shares the LEAN-style pipeline with production.
   - `elite_flow` and `yolo_momentum` are standalone loops with their own data, signals, order handling, and state.
   - The highest-risk live code is therefore not fully constrained by the best backtest architecture.

4. Execution model is too optimistic for very high leverage.
   - The standard simulated execution has fee/slippage, but not liquidation path, exchange max leverage tiers, stop-order trigger failure, partial fills, latency spikes, or gap-through-stop behavior.
   - YOLO Monte Carlo models some path risk, but it is a separate simulator.

5. Risk is split between portfolio-level controls and strategy-local emergency logic.
   - Portfolio risk stack is composable.
   - YOLO/Elite loops also carry local stops, leverage, target ROI, and liquidation checks.
   - There is no single account-level risk arbiter that sees all orders before submission.

6. Observability exists but is not yet a research feedback loop.
   - Logs and dashboard are useful for runtime monitoring.
   - Missing: structured decision journal that joins market features, signal scores, intended order, actual fill, later outcome, and post-trade attribution.

7. Repo docs have path drift.
   - `AGENTS.md` references `.Codex`, but actual tools and knowledge are in `.claude`.
   - Logged as `DOC_PATH_DRIFT`.

## Data Gaps

Already present:
- 1h OHLCV/funding cache for many USDT futures.
- Live tickers with bid/ask/volume.
- Order book snapshots.
- Current funding and OI via OKX public endpoints.

Missing or under-modeled:
- Historical order book depth / imbalance snapshots beyond live memory.
- Historical trades / taker buy-sell imbalance / CVD.
- Historical open interest series for the full universe.
- Historical long-short ratio and account-position ratio.
- Mark/index/premium history as explicit features.
- Liquidation proxy data: not directly available from OKX kit; must be approximated from price/OI/funding/volume or sourced externally.
- Per-instrument contract metadata snapshots over time: max leverage, tick size, lot size, min size, max market size.
- Real fill dataset: order request, exchange response, fill price, latency, slippage vs mid, post-fill adverse excursion.
- Universe membership snapshots so newly listed symbols do not leak into old historical windows.

## Feature Research Roadmap

Phase 1: Build canonical feature/label datasets.
- Add `engine/features/` with feature builders for OHLCV, funding, OI, order book, and universe metadata.
- Add `engine/research/datasets.py` to materialize panel data keyed by `(timestamp, inst_id)`.
- Add label builders: forward return, max favorable/adverse excursion, hit TP before SL, liquidation-distance breach, breakout continuation.
- Save feature versions to parquet with metadata JSON.

Phase 2: Feature families worth testing first.
- Momentum: multi-horizon returns, EMA slope, breakout distance, volatility-adjusted trend, acceleration.
- Microstructure: spread bps, top-N depth imbalance, OFI, book slope, trade CVD, taker imbalance.
- Derivatives crowding: funding z-score, funding slope, OI change, OI-price divergence, premium z-score, long-short ratio z-score.
- Regime: BTC/ETH trend state, market breadth, cross-sectional dispersion, realized vol percentile, correlation cluster state.
- Execution risk: spread, depth at target notional, expected slippage, min size/contract granularity, max leverage cap.
- Event/listing behavior: age since listing, volume shock, new-contract mania filters.

Phase 3: Feature selection.
- Start with univariate IC / rank IC by horizon and regime.
- Add purged walk-forward evaluation; no random split.
- Use stability filters: sign consistency, turnover cost, performance by symbol bucket, performance by volatility regime.
- Only after stable simple signals, use lightweight models such as regularized logistic/linear models or gradient boosted trees.

## Small-Capital High-Risk Notes

- With about 1000 USDT and high risk tolerance, edge must be concentrated, but the system should still avoid unrecoverable single bad fills.
- The best use of risk is not just lower leverage; it is better trade selection, smaller loss per wrong hypothesis, and fast disqualification of bad market states.
- Martingale can maximize contest-style median outcomes but has negative-tail convexity. Current MC results show high success rates but very large 5th percentile and mean drawdown damage; this should be treated as lottery architecture, not a stable quant engine.

## Highest Priority Next Work

1. Create a feature store and label builder.
2. Bring YOLO/Elite feature computation into reusable feature modules.
3. Add an account-level pre-trade risk arbiter before every ATK order.
4. Build historical OI/funding/premium collection scripts for the whole active universe.
5. Add a real decision journal that joins features, signals, orders, fills, and later outcomes.
6. Fix or quarantine known `CAGR_METRIC_ZERO` before using CAGR in reports.
7. Pin or upgrade OKX CLI version; current observed version is `1.2.7`, while CLI reports newer versions available.
