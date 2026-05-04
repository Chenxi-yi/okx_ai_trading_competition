# Monster Feature Experiment Log

This file is the persistent registry for tested and proposed monster-coin
feature combinations. Update it whenever a feature set, gate, rule, or
execution parameter set is tested.

## 2026-04-27 19:06 CST — Log Created

### Tested Feature Sets

#### OHLCV Monster Score V1

- Construction:
  - Source: 5m OHLCV history across the broad swap universe.
  - Positive labels: mined monster episodes from historical 5m bars.
  - Feature families:
    - short/medium returns: `ret_15m`, `ret_1h`, `ret_3h`, `ret_6h`, `ret_12h`, `ret_24h`, `ret_3d`, `ret_5d`, `ret_7d`
    - relative volume: `rvol_*`
    - range expansion: `range_pct_*`
    - distance from recent high: `dist_high_*`
    - cross-sectional ranks and idiosyncratic returns
    - market breadth/event filters
  - Scoring: univariate AUC-ranked features, percentile score, weighted by AUC distance.
- Current best historical lottery execution result:
  - Signal table: `monster_signal_table_2026q1q2_20260426`
  - Sweep: `monster_lottery_signal_sweep_2026q1q2_20260426`
  - Best config:
    - `risk_budget=20`
    - `long_score=0.95`
    - `stop_loss=0.15`
    - `tp1=0.50`
    - `tp2=1.50`
    - `runner_trailing=0.30`
  - Performance:
    - final NAV `1211.65` from `1000.00`
    - return `+21.16%`
    - max drawdown `-9.94%`
    - trade events `24`
    - win rate `50.0%`
    - profit factor `2.59`
    - best return on risk budget `4.48x`
    - max consecutive losing events `4`
- Interpretation:
  - Low thresholds `0.88/0.90` overtrade.
  - Selective score threshold around `0.95` plus wide runner works better.

#### Live Liquidity + Derivatives Gate V1

- Construction:
  - Source: candidate-only live snapshots.
  - Orderbook features:
    - `spread_bps`
    - `depth_1pct_usd`
    - `top_imbalance`
    - `depth_imbalance_*`
    - `microprice_vs_mid_bps`
  - Derivatives features:
    - `open_interest_value`
    - `funding_rate`
    - `long_short_ratio`
  - Default gate:
    - `max_snapshot_age_minutes=180`
    - `max_spread_bps=20`
    - `min_depth_1pct_usd=10000`
    - `min_open_interest_value=1000000`
    - `max_abs_funding_rate=0.0015`
    - `0.25 <= long_short_ratio <= 4.0`
- Smoke result:
  - Fresh candidates tested: `AXS/USDT`, `CORE/USDT`, `APE/USDT`, `PIPPIN/USDT`, `API3/USDT`, `SOON/USDT`.
  - Passed: `5/6`.
  - Rejected: `PIPPIN/USDT`, reason `long_short>4`.
  - Paper lottery with live gate opened `AXS/USDT`, `CORE/USDT`, `APE/USDT`.
- Interpretation:
  - The gate catches at least one crowded candidate while allowing liquid/high-OI names.
  - Needs live paper history before judging PnL impact.

#### Live Liquidity + Derivatives Gate V1 On Auto-Refreshed Watchlist

- Timestamp: 2026-04-27 21:37 CST.
- Watchlist source: `monster_auto_fixed_watchlist_20260427T112037Z`.
- Candidate set:
  - `ZBT/USDT`
  - `BICO/USDT`
  - `PIPPIN/USDT`
  - `RAVE/USDT`
  - `MERL/USDT`
  - `APE/USDT`
  - `CORE/USDT`
  - `BIO/USDT`
  - `ENSO/USDT`
- Live data sources:
  - Orderbook: `monster_orderbook_live_20260427_211706`
  - Derivatives: `monster_derivatives_live_20260427_211706`
- Gate result:
  - Passed `5/9`: `APE/USDT`, `BIO/USDT`, `CORE/USDT`, `ENSO/USDT`, `MERL/USDT`.
  - Rejected `4/9`:
    - `BICO/USDT`: `oi_value<1e+06; abs_funding>0.0015`
    - `PIPPIN/USDT`: `long_short>4`
    - `RAVE/USDT`: `depth_1pct<10000`
    - `ZBT/USDT`: `depth_1pct<10000`
- Interpretation:
  - The gate currently rejects exactly the kinds of names that are structurally harder to trade with a small account: shallow book, low OI, expensive funding, or crowded positioning.
  - Need longer paper history to determine whether `depth_1pct<10000` is too strict for 10-20 USDT lottery sizing.

### Proposed Next Feature Tests

- OI acceleration:
  - `oi_value_change_5m/15m/1h`
  - `oi_value_zscore`
  - `price_up + oi_up` versus `price_up + oi_down`
- Funding crowding:
  - high positive funding as long-entry penalty
  - high negative funding as short-squeeze candidate boost
- Orderbook pressure:
  - `depth_imbalance_50bps/1pct`
  - `microprice_vs_mid_bps`
  - spread widening before breakout or dump
- Breakout quality:
  - strong `rvol_6h + range_pct_6h`
  - but reject if `ret_1h` already too extended
- Rotation context:
  - cross-sectional rank of `ret_24h`
  - market breadth filters during broad selloff/pump
- Exit features:
  - live liquidity deterioration while in position
  - OI collapse after pump
  - funding flip against position
