# Quant Strategy Implementation Guide

> NOTE: The promotion criteria below are for PRODUCTION live trading only.
> The current system runs on Binance TESTNET (sandbox) with fake money.
> Testnet execution is always allowed — no paper trading period required.

This document defines the phase-1 strategy set for this repository.

It is not a literature survey. It is an implementation contract for AI agents and engineers building:

- strategy modules
- signal generation
- parameter tuning
- backtesting
- portfolio construction
- live execution and monitoring

The target setup is:

- Venue: Binance
- Capital: `USD 5,000`
- Instruments: `USDT-M` perpetual futures first, spot only as fallback
- Trading style: `24/7`, low-to-medium turnover, systematic, long/short where possible
- Objective: `>15% annualized` net return over full-cycle evaluation
- Secondary objective: maximize net Sharpe, but treat `2.5-3.0` as an aspirational portfolio-level goal rather than a single-strategy requirement

## 1. Strategic Scope

### 1.1 Approved Phase-1 Strategies

Build only these three strategies first:

1. Cross-Sectional Momentum
2. Time-Series Trend / Momentum
3. Funding-Aware Carry

These three are the best fit for:

- small starting capital
- Binance market structure
- futures-native long/short expression
- manageable data and infrastructure requirements
- realistic live deployment for a first 24/7 system

### 1.2 Explicitly Deprioritized

Do not make these phase-1 priorities:

- high-frequency market making
- limit-order-book deep learning
- pure chart-pattern trading
- equity-style statistical arbitrage copied directly from stock-market papers

They can remain in the codebase as research or backlog modules, but they should not be treated as core alpha engines for initial production deployment.

## 2. Global Design Rules

These rules apply to every strategy module.

### 2.1 Instrument Universe

Primary live universe:

- `BTCUSDT`
- `ETHUSDT`
- `SOLUSDT`
- `BNBUSDT`
- `ADAUSDT`
- `AVAXUSDT`

Expanded research universe once the pipeline is stable:

- add liquid `USDT-M` perps only
- require minimum daily dollar volume
- require continuous listing history long enough for the strategy lookback
- exclude low-liquidity or structurally unstable contracts

### 2.2 Trading Frequency

Default signal frequencies:

- `4h`
- `8h`
- `1d`

Do not start with sub-minute strategies.

### 2.3 Execution Assumptions

Default to conservative execution assumptions:

- use taker fees in backtests unless a maker-fill model is explicitly implemented
- include spread crossing
- include slippage that increases with volatility and participation rate
- include funding payments for perpetual futures
- include missed fills when trading on breakouts or rebalances

### 2.4 Portfolio Construction

Use strategy sleeves, not a winner-take-all single model.

Portfolio rules:

- each strategy outputs normalized target weights, not raw order sizes
- combine sleeves using explicit allocation weights
- volatility-scale each sleeve before combining
- enforce gross, net, per-asset, and sector-style concentration caps

### 2.5 Risk Philosophy

Risk management is not a stop-loss bolt-on. It is part of the signal implementation.

Minimum live constraints:

- max gross leverage
- max position size per asset
- max sleeve allocation
- max daily loss threshold
- drawdown circuit breaker
- liquidation distance guard
- funding-cost guard
- stale-data / exchange-connectivity guard

## 3. Standard Strategy Module Contract

Every strategy module should support the same structured interface.

### 3.1 Required Inputs

- OHLCV bars
- funding rates for futures strategies
- contract metadata
- fee schedule
- slippage model parameters
- universe membership filter

### 3.2 Required Outputs

Each strategy must return, per timestamp and symbol:

- `signal_direction` in `{-1, 0, +1}`
- `signal_strength` in `[0, 1]`
- `target_weight`
- `confidence`
- `metadata`

`metadata` should include:

- sub-signals used
- lookbacks
- realized volatility estimate
- turnover estimate
- funding filter status

### 3.3 Required Methods

Each strategy implementation should expose methods equivalent to:

- `prepare_features`
- `generate_raw_signals`
- `apply_filters`
- `scale_positions`
- `get_target_weights`
- `parameter_grid`
- `validate_backtest_inputs`

## 4. Shared Backtest Standards

All strategy backtests must follow these standards.

### 4.1 Data Handling

- use chronologically correct point-in-time data only
- align all symbols to a common timestamp index
- remove symbols that do not meet history requirements at a given date
- do not forward-fill funding data across unreasonable gaps

### 4.2 Cost Model

Backtests must include:

- trading fees
- slippage
- spread cost
- funding payments
- borrow cost if spot margin is ever used

### 4.3 Validation Scheme

Use walk-forward testing only.

Acceptable validation structures:

- anchored expanding window
- rolling walk-forward window

Do not use random train/test splits.

### 4.4 Reported Metrics

Each backtest report must include at least:

- annualized return
- annualized volatility
- Sharpe ratio
- Sortino ratio
- max drawdown
- Calmar ratio
- hit rate
- profit factor
- turnover
- average holding period
- exposure by asset
- total fees
- total funding paid/received

### 4.5 Stress Tests

Every strategy must be re-run under:

- doubled fee assumptions
- doubled slippage assumptions
- reduced universe size
- delayed execution by one bar
- top-2 assets removed
- high-volatility subperiod only

If the edge disappears under small perturbations, the strategy is too fragile.

## 5. Parameter Tuning Rules

Parameter tuning is constrained optimization, not free-form search.

### 5.1 Allowed Tuning Process

- define a small, economically sensible parameter grid
- tune only on training windows
- select parameters using net performance after costs
- prefer robust plateaus over sharp optima
- freeze parameters before the test window

### 5.2 Forbidden Tuning Behavior

- tuning on full-sample results
- changing logic after seeing test results without rerunning the full walk-forward process
- optimizing on Sharpe alone while ignoring turnover and drawdown
- using hundreds of loosely justified combinations

### 5.3 Strategy Selection Rule

Promote a strategy only if it shows:

- positive net return in most walk-forward folds
- stable behavior across parameter neighborhoods
- acceptable drawdown and turnover
- no dependence on one symbol or one regime

## 6. Strategy 1: Cross-Sectional Momentum

### 6.1 Objective

Exploit relative-strength dispersion across liquid crypto perpetuals by going long stronger assets and short weaker assets while controlling market beta and volatility.

### 6.2 Why This Strategy

This is a better crypto-native replacement for equity-style statistical arbitrage. It uses the same cross-sectional ranking idea but avoids forcing stock-market assumptions onto crypto.

### 6.3 Data Frequency

Primary:

- `1d`

Secondary research:

- `8h`

### 6.4 Universe Rule

At each rebalance date:

- include only symbols with sufficient lookback history
- include only symbols above liquidity threshold
- exclude newly listed contracts until seasoning period is met

### 6.5 Feature Set

Core features:

- trailing return over `7d`
- trailing return over `30d`
- trailing return over `90d`
- realized volatility over `20d`
- momentum acceleration: `7d return - 30d return`
- volume z-score
- funding-rate z-score

Optional later:

- market beta to BTC
- downside semivolatility
- open-interest change if reliable data is added

### 6.6 Signal Logic

Base score:

`score = w1 * rank(ret_7d) + w2 * rank(ret_30d) + w3 * rank(ret_90d) - w4 * rank(vol_20d) - w5 * abs(rank(funding_z))`

Default first version:

- equal weights on `7d`, `30d`, `90d` momentum
- mild penalty on extreme volatility
- mild penalty on extreme funding

Portfolio signal:

- long top `N_long`
- short bottom `N_short`
- ignore middle-ranked assets

Default direction rules:

- if universe size `< 6`, reduce breadth or stay flat
- optionally require absolute momentum filter for longs and shorts

### 6.7 Position Sizing

Use volatility-targeted cross-sectional weights:

- inverse-volatility weights within long basket
- inverse-volatility weights within short basket
- normalize long and short books to target gross exposure
- beta-neutralize to BTC if feasible

Default starting constraints:

- target sleeve volatility: `8-12%` annualized
- per-asset notional cap: `20%` of NAV
- gross sleeve cap: `100-150%`
- net exposure target: near `0`

### 6.8 Risk Instructions

Required controls:

- no position if funding is extreme against the position
- no position if realized vol exceeds hard threshold
- drop symbol if spread/slippage model exceeds cap
- stop trading sleeve if cross-sectional breadth collapses

Live risk guard examples:

- skip longs with highly positive funding beyond threshold
- skip shorts with highly negative funding beyond threshold
- reduce gross exposure when BTC market volatility spikes

### 6.9 Backtest Instructions

Rebalance options to test:

- daily
- every `3` days
- every `7` days

For each rebalance:

- rank symbols using only prior-bar data
- apply execution lag of `1` bar in the main backtest
- charge full transaction costs on turnover
- accrue funding between rebalance points

Backtest outputs must include:

- factor return by long leg
- factor return by short leg
- breadth over time
- turnover decomposition

### 6.10 Parameter Fine-Tuning Instructions

Tune only the following:

- momentum lookbacks: `7d`, `14d`, `30d`, `60d`, `90d`
- rebalance frequency
- number of names long and short
- volatility lookback
- funding penalty threshold

Do not tune:

- arbitrary nonlinear score formulas
- huge weight combinations

Selection priority:

1. net Sharpe after costs
2. turnover efficiency
3. stability across folds
4. drawdown control

### 6.11 Expected Failure Modes

- strong one-way market where short basket squeezes
- universe too small for robust cross-sectional dispersion
- high funding drag on crowded shorts or longs
- edge concentrated in one altcoin regime

## 7. Strategy 2: Time-Series Trend / Momentum

### 7.1 Objective

Capture medium-term directional persistence in liquid crypto futures using simple, robust trend signals.

### 7.2 Why This Strategy

This is the highest-priority strategy for initial deployment. It is easier to validate, easier to operate, and more resilient than HFT or deep-learning approaches.

### 7.3 Data Frequency

Primary:

- `4h`
- `1d`

### 7.4 Signal Components

Use a small ensemble of interpretable sub-signals:

1. Dual moving average crossover
2. Breakout / channel signal
3. Simple time-series momentum sign

### 7.5 Signal Definitions

#### A. Dual Moving Average Crossover

Long if:

- `fast_ma > slow_ma * (1 + band)`

Short if:

- `fast_ma < slow_ma * (1 - band)`

Else:

- flat

#### B. Breakout Signal

Long if:

- close breaks above prior `N`-bar high

Short if:

- close breaks below prior `N`-bar low

Else:

- hold prior state or go flat depending on implementation variant

#### C. Time-Series Momentum

Long if:

- trailing return over lookback `L` is positive

Short if:

- trailing return over lookback `L` is negative

### 7.6 Ensemble Rule

Base version:

- each sub-signal outputs `{-1, 0, +1}`
- ensemble signal is sign of the average

Optional later:

- weight sub-signals by trailing out-of-sample information ratio

### 7.7 Position Sizing

- estimate realized volatility per asset
- scale each position to target constant per-asset risk
- cap leverage and per-asset notional

Default starting constraints:

- target sleeve volatility: `8-10%`
- per-asset cap: `25%` NAV
- gross sleeve cap: `100%`

### 7.8 Risk Instructions

Required controls:

- avoid immediate re-entry after stop-out if volatility regime is unstable
- reduce position size when funding is punitive
- reduce or disable alt exposure when BTC volatility regime is extreme
- use trailing volatility stop or time stop for stale positions

Do not use very tight hard stops that turn a trend strategy into a noise-harvesting strategy.

### 7.9 Backtest Instructions

Run separate tests for:

- `4h` bars
- `1d` bars

Test on:

- individual assets
- equal-weight basket
- volatility-weighted basket

Backtest requirements:

- one-bar lag after signal generation
- costs charged on position changes
- funding accrued while positions remain open
- regime segmentation by market state

### 7.10 Parameter Fine-Tuning Instructions

Tune only:

- fast MA: `10, 20, 30`
- slow MA: `50, 100, 150, 200`
- breakout lookback: `20, 50, 100`
- TSMOM lookback: `20, 60, 120`
- band filter: `0, 0.5%, 1.0%`
- volatility lookback: `20, 40, 60`

Selection rules:

- choose parameter regions that work across multiple assets
- prefer lower turnover if net performance is similar
- prefer `1d` if `4h` edge is mostly eaten by costs

### 7.11 Expected Failure Modes

- whipsaw markets
- low-dispersion sideways periods
- overstaying trends into reversal if funding drag is ignored

## 8. Strategy 3: Funding-Aware Carry

### 8.1 Objective

Monetize persistent carry or basis effects in perpetual futures while limiting exposure to trend shocks and crowded positioning.

### 8.2 Why This Strategy

Perpetual futures have a structural funding mechanism. Even if raw carry is not always exploitable on its own, funding is important enough to be treated as either:

- a standalone sleeve
- or a portfolio filter over trend and cross-sectional momentum

### 8.3 Data Frequency

Primary:

- `8h`
- `1d`

This aligns naturally with Binance funding intervals.

### 8.4 Signal Logic Variants

Implement two variants and test them separately.

#### Variant A: Pure Carry

Long assets with sufficiently negative expected funding and short assets with sufficiently positive expected funding, subject to liquidity and trend filters.

#### Variant B: Funding-Aware Overlay

Use funding as a veto or scaling layer:

- reduce long size when funding is strongly positive
- reduce short size when funding is strongly negative
- increase conviction only when base signal and funding are aligned

Variant B is likely more robust as an early production implementation.

### 8.5 Feature Set

- current funding rate
- trailing average funding over `3`, `7`, `14` funding windows
- funding z-score
- basis proxy if available
- realized volatility
- recent price trend

### 8.6 Position Rules

For pure carry:

- require funding to exceed threshold in absolute value
- require volatility below cap
- require trend filter not strongly against the position

For overlay version:

- apply funding multiplier to trend or cross-sectional target weight

Example:

`adjusted_weight = base_weight * funding_multiplier`

where `funding_multiplier` decreases as expected funding cost rises against the trade.

### 8.7 Risk Instructions

Carry is vulnerable to violent squeezes. Required controls:

- hard cap on exposure per symbol
- trend veto for extreme countertrend carry trades
- event volatility kill switch
- funding regime cap so the strategy does not chase extreme crowding

### 8.8 Backtest Instructions

Backtest logic must:

- accrue actual or modeled funding at funding timestamps
- separate PnL into price PnL and funding PnL
- report whether returns come from carry capture or directional drift

Required comparisons:

- pure carry alone
- trend alone
- trend plus funding overlay
- cross-sectional momentum plus funding overlay

### 8.9 Parameter Fine-Tuning Instructions

Tune only:

- funding averaging window
- threshold for actionable funding
- trend veto threshold
- overlay multiplier schedule
- holding horizon

Primary evaluation criterion:

- incremental improvement to portfolio Sharpe and drawdown, not standalone CAGR only

### 8.10 Expected Failure Modes

- extreme trending markets where carry remains expensive for long periods
- funding reversals
- crowded short squeezes
- insufficient edge after fees if traded too frequently

## 9. Portfolio Combination Rules

The portfolio should combine sleeves in stages.

### 9.1 Initial Portfolio Weights

Recommended starting allocation:

- `50%` Time-Series Trend / Momentum
- `35%` Cross-Sectional Momentum
- `15%` Funding-Aware Carry

These are sleeve risk budgets, not cash allocations.

### 9.2 Combination Logic

At each rebalance:

1. compute target weights per sleeve
2. volatility-scale each sleeve to its risk budget
3. sum target weights
4. clip to portfolio gross/net constraints
5. apply exchange and liquidation constraints

### 9.3 Portfolio-Level Limits

Starting limits:

- max gross leverage: `1.5x`
- max net exposure: `0.5x`
- max single-asset exposure: `0.25x`
- max daily portfolio loss before de-risking: `2%`
- max portfolio drawdown before circuit breaker: `10-12%`

## 10. Implementation Roadmap

### 10.1 Phase 1

Build in this order:

1. Time-Series Trend / Momentum
2. Cross-Sectional Momentum
3. Funding overlay on top of both
4. Funding standalone sleeve
5. Portfolio combiner

### 10.2 Codebase Mapping

Recommended module targets:

- `strategies/trend_momentum.py`
- `strategies/cross_sectional_momentum.py`
- `strategies/funding_carry.py`
- `signals/combiner.py`
- `portfolio/portfolio_manager.py`
- `risk/risk_manager_v2.py`
- `backtest/engine.py`
- `backtest/metrics.py`

### 10.3 Legacy Modules

Treat these as non-core research modules:

- `strategies/ml_stat_arb.py`
- `strategies/market_making.py`
- `strategies/lob_forecasting.py`
- `strategies/pattern_recognition.py`

They should not drive production design decisions for the first live system.

## 11. Promotion Criteria To Paper Trading

A strategy may move to paper trading only if:

- net backtest results remain positive after conservative costs
- walk-forward results are stable
- turnover is operationally manageable
- exposure is not concentrated in one symbol
- stress tests do not fully erase the edge
- implementation logs and diagnostics are complete

## 12. Promotion Criteria To Live Trading

Move from paper to live only if:

- at least one month of clean paper execution exists
- order lifecycle and reconciliation are correct
- funding accrual is validated
- risk guardrails trigger correctly in simulation and paper mode
- position sizing remains conservative

Start live with smaller effective risk than the backtest target.
