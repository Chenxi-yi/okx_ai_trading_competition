# Quantitative Crypto Trading Framework

This repository is being organized around a realistic phase-1 objective:

- Venue: Binance
- Capital: `USD 5,000`
- Products: `USDT-M` perpetual futures first
- Style: 24/7 systematic trading
- Return target: `>15%` annualized after fees and slippage
- Risk target: maximize net Sharpe through diversified sleeves and strict risk controls

The repository should optimize for robust, codeable, low-to-medium turnover strategies rather than research-heavy HFT or deep-learning microstructure projects.

## Phase-1 Strategy Set

The primary strategies for this repo are:

1. Time-Series Trend / Momentum
2. Cross-Sectional Momentum
3. Funding-Aware Carry

These are specified in [strategies-guide.md](/Users/lucaslee/quant_trade_competition/engine/strategies-guide.md).

Legacy modules such as market making, LOB forecasting, pattern recognition, and equity-style ML stat arb should be treated as research backlog unless they are explicitly reworked to fit the crypto-first design.

## Design Principles

- Prefer `USDT-M` perpetuals over spot margin for clean long/short expression.
- Use `4h`, `8h`, and `1d` bars before attempting faster execution.
- Backtest with conservative costs: fees, spread, slippage, and funding.
- Use walk-forward validation only.
- Combine strategy sleeves through portfolio risk budgeting instead of relying on one model.
- Make every strategy emit target weights plus diagnostics, not just direction labels.

## Current Code Areas

Core folders already present:

- `strategies/`
- `backtest/`
- `risk/`
- `portfolio/`
- `execution/`
- `data/`
- `signals/`

Recommended phase-1 build order:

1. finish or refactor `strategies/trend_momentum.py`
2. add `strategies/cross_sectional_momentum.py`
3. add `strategies/funding_carry.py`
4. update `signals/combiner.py` for sleeve aggregation
5. update risk, portfolio, and backtest layers to support target weights, funding accrual, and conservative execution assumptions

## Minimum Backtest Requirements

Every strategy backtest should report:

- annualized return
- annualized volatility
- Sharpe ratio
- max drawdown
- turnover
- fees paid
- funding paid or received
- exposure by asset

Every backtest should also be stress-tested under:

- higher fees
- higher slippage
- one-bar execution delay
- reduced universe size

## Live Deployment Standard

Do not promote a strategy directly from a promising backtest to live trading.

Required progression:

1. walk-forward backtest
2. paper trading
3. reconciliation and risk validation
4. small-size live deployment

## Next Implementation Target

The next concrete build target for this repo should be:

- an implementation-ready `trend_momentum` sleeve
- a new `cross_sectional_momentum` sleeve
- a `funding_carry` overlay or sleeve
- portfolio combination logic with volatility targeting and exposure caps

The detailed build contract is in [strategies-guide.md](/Users/lucaslee/quant_trade_competition/engine/strategies-guide.md).
