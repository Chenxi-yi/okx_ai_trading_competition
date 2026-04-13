# Quantitative Trading Philosophy and Operating Model

## Executive Summary

This strategy is built as a systematic, risk-budgeted crypto portfolio for 24/7 markets, with Binance `USDT-M` perpetual futures as the primary venue. The design objective is not to chase isolated high-conviction bets or discretionary narratives. It is to compound capital through a diversified set of repeatable, testable return sources while controlling downside through explicit portfolio constraints, dynamic risk throttles, and conservative execution assumptions.

The current production philosophy is intentionally narrow:

- trade only liquid major crypto contracts
- use medium-frequency signals rather than fragile high-frequency microstructure edges
- combine multiple independent sleeves instead of relying on one model
- size positions from risk, not conviction alone
- treat backtests as adversarial validation exercises, not marketing material
- keep live deployment operationally simple enough to monitor from a phone through Claude Code

This is a research-driven trading system, but it is engineered with operator control in mind. The goal is robust return generation after fees, slippage, and funding, not theoretical paper alpha.

## Market Thesis

Crypto is structurally suitable for systematic trading because it is:

- continuously traded, allowing stable daily and multi-day signal generation
- broad enough cross-sectionally to support relative-value ranking
- sentiment- and flow-driven enough for trend persistence
- structurally shaped by funding mechanics, creating a carry source absent in many cash markets

These characteristics support three complementary sources of expected return:

1. Time-series trend and momentum
2. Cross-sectional relative strength and weakness
3. Funding-aware carry

Those are the sleeves implemented as the phase-1 portfolio. Legacy modules such as market making, LOB forecasting, pattern recognition, and ML stat arb remain research backlog and are not the core capital allocation thesis today.

## Signal Science and Math

### 1. Trend / Time-Series Momentum Sleeve

The trend sleeve is designed to capture persistence in directional moves across the core futures universe.

It combines three sub-signals:

- Dual moving average crossover
- Breakout confirmation
- Time-series momentum

The moving-average component compares faster windows against slower windows and only triggers when the fast average exceeds the slow average by a small band. This suppresses low-quality crossover noise and acts as a basic hysteresis filter.

The breakout component compares current price with rolling prior highs and lows using shifted windows, which is important because it prevents the signal from using the same bar both to define and to break the level.

The momentum component uses the sign of returns over multiple horizons. Rather than betting on one lookback, it averages directional information across several windows, which makes the signal less sensitive to a single regime.

At the portfolio level, the sleeve computes:

`raw_score = (dmac + breakout + momentum) / 3`

That raw directional score is then scaled by inverse realized volatility. In effect, the sleeve expresses the view:

- if two assets have equally strong trend evidence, the lower-volatility asset should generally carry more weight
- if volatility rises, gross allocation should contract automatically before portfolio-level controls apply

### 2. Cross-Sectional Momentum Sleeve

The cross-sectional sleeve is a relative-value ranking model across the crypto universe.

It does not ask, "Is BTC going up?" It asks, "Which assets are strongest and weakest versus peers?" That distinction matters because cross-sectional momentum can still create opportunity when the overall market is noisy.

The sleeve:

- computes returns across several lookback horizons
- ranks assets cross-sectionally by percentile on each date
- averages those percentile ranks into a composite score
- goes long the top-ranked names
- goes short the bottom-ranked names when the mode supports shorting

This is a robust and interpretable approach. It avoids overfitting to a single signal transform and instead uses relative ranking, which tends to be more stable than raw score comparisons in noisy markets.

Again, inverse-volatility scaling is applied before normalization so that sleeve risk is distributed more evenly across names.

### 3. Funding-Aware Carry Sleeve

Perpetual futures introduce a structurally important signal: funding.

Funding carry is based on the idea that:

- when funding is strongly positive, longs are paying shorts
- when funding is strongly negative, shorts are paying longs

That payment flow is itself a potential return source, but naive carry strategies are dangerous because extreme funding often coincides with strong directional crowding. The sleeve therefore does not trade carry in isolation. It applies a trend veto.

The logic is:

- smooth funding over multiple windows
- require funding imbalance to exceed a threshold before acting
- only go long when funding is sufficiently negative and medium-term trend is not adverse
- only go short when funding is sufficiently positive and medium-term trend is not adverse

This makes the carry sleeve a selective crowding premium capture model rather than a blunt mean-reversion bet.

## How Signals Become Portfolio Weights

Each sleeve outputs:

- direction
- strength
- target weights
- confidence
- metadata for diagnostics

This is an important design decision. The system does not emit only "buy" or "sell" labels. It emits target portfolio weights, which lets the execution layer rebalance capital as a portfolio optimizer rather than as a binary order router.

The sleeves are combined through explicit capital budgeting:

- Trend: 50%
- Cross-sectional: 35%
- Carry: 15%

The combined weight matrix is an average of sleeve outputs under those weights. This means the portfolio is diversified by construction across independent signal families rather than being concentrated in whichever model is loudest on a given day.

## Portfolio Construction Philosophy

The portfolio is built around risk constraints first and signal views second.

Core portfolio rules:

- maximum gross leverage: 1.5x
- maximum net exposure: 50%
- maximum single-position size: 25% of NAV
- minimum rebalance notional: USD 10

These constraints serve different purposes:

- gross leverage limits total book size
- net exposure prevents uncontrolled directional drift
- single-name caps reduce concentration risk
- minimum rebalance notional suppresses micro-trading and fee churn

The result is a portfolio that can express conviction but is structurally prevented from becoming a disguised one-bet book.

## Risk Management Framework

The risk process is not a single stop-loss rule. It is layered.

### 1. ATR-Based Dynamic Stops

The system computes Average True Range and converts it into dynamic stop distance:

`stop_distance = ATR_multiplier * ATR`

This matters because fixed-percentage stops are too tight in high-volatility regimes and too loose in calm ones. ATR-based stops adapt to market state.

### 2. Drawdown Circuit Breakers

The portfolio tracks high-water mark and enforces two drawdown thresholds:

- first threshold: reduce risk
- second threshold: go to cash

This is a portfolio-level kill switch. It assumes that when drawdown deepens beyond acceptable limits, the correct action is not to argue with the market but to reduce exposure mechanically.

The circuit breaker also includes reset logic after recovery or cooldown, which prevents permanent lockout after a severe drawdown event.

### 3. Volatility Regime Detection

The system classifies current realized portfolio volatility into:

- low
- medium
- high

Position size is then scaled by regime:

- low vol: modest upsize
- medium vol: neutral
- high vol: defensive downsize

This is a macro risk throttle that helps avoid carrying full exposure into structurally unstable periods.

### 4. Correlation Watchdog

When cross-asset correlations compress upward, apparent diversification can vanish. The system checks rolling average pairwise correlation across the book and cuts size when systemic clustering risk becomes too high.

This is especially important in crypto, where markets can look diversified during normal periods but collapse into one factor during stress.

### 5. Exchange Reconciliation

Live state is not blindly trusted. Before each run, the system reconciles local state with exchange positions and cash, treating the exchange as source of truth.

That reduces operational drift between internal accounting and real exposures.

## Execution and Cost Discipline

This process assumes that execution costs are real and must be modeled before capital is deployed.

The backtest and live accounting include:

- transaction fees
- slippage
- funding cashflows
- turnover tracking

Slippage is modeled through a square-root market impact function:

`slippage ~ factor * sqrt(order_size / average_daily_volume)`

That is a better approximation than assuming constant basis-point impact regardless of size. It penalizes larger trades in thinner instruments more heavily, which is closer to real execution.

The system also uses tradeability filters:

- minimum listing history
- minimum average daily dollar volume
- valid open and close data availability

Assets that fail these filters are simply not eligible for trading. This helps prevent backtests from overstating returns by trading illiquid tails of the universe.

## Backtesting Methodology

The backtest engine is designed to approximate implementable trading, not perfect hindsight.

### Core Mechanics

- signals are generated from historical data
- target weights are shifted by one bar before execution
- trades are executed using execution prices distinct from marking prices
- funding is applied with a lag
- positions are rebalanced only if size change exceeds a minimum threshold

These mechanics matter because many backtests fail by quietly cheating:

- using same-bar information for same-bar execution
- ignoring costs
- assuming infinite liquidity
- overstating diversification

This framework explicitly avoids those fallacies.

## How the Framework Avoids Common Quant Fallacies

### Look-Ahead Bias

The system shifts target weights by one bar before rebalancing. In plain terms: today’s portfolio is based on yesterday’s information, not tomorrow’s.

Breakout levels are also computed from shifted rolling windows so a bar does not define and break its own signal threshold simultaneously.

Funding is lagged before application as well.

### Survivorship and Availability Bias

The universe is restricted to liquid major contracts with actual price and volume history. Assets without enough history or sufficient average dollar volume are filtered out.

The system therefore avoids manufacturing performance from instruments that were not realistically tradable at the time.

### Turnover Blindness

Small rebalances below the USD 10 threshold are ignored. This keeps the model from pretending that tiny daily adjustments are free.

Fees, slippage, and funding are tracked explicitly, so gross alpha is not confused with net investor return.

### Overfitting

The optimization stack uses walk-forward validation instead of static in-sample fitting.

It also computes anti-overfit diagnostics such as:

- in-sample versus out-of-sample Sharpe degradation
- parameter stability across folds
- average score gap between winner and median candidate
- negative validation objective warnings

That is the correct mindset: optimization is a controlled search process, not a fishing expedition for a backtest that looks good in one sample.

### False Diversification

The portfolio does not assume that multiple crypto assets are independent just because they have different tickers. It measures correlations and reduces size when diversification quality degrades.

## Performance Measurement Philosophy

The system evaluates itself using investor-relevant metrics, including:

- annualized return
- annualized volatility
- Sharpe ratio
- Sortino ratio
- Calmar ratio
- max drawdown
- turnover
- total fees
- total slippage
- total funding
- per-trade win rate and profit factor
- monthly return table

The point is not to optimize one vanity metric. A strategy that looks good on return but requires intolerable drawdowns, unstable turnover, or unrealistic cost assumptions is not investor-grade.

## Live Deployment on Claude Code

The live architecture is intentionally simple and operator-friendly.

### Operating Model

The trading engine is packaged as a single-run command interface:

- `start`
- `rebalance`
- `status`
- `stop`

This is a strong operational choice because it avoids hidden daemon complexity. Each run is explicit and observable.

The live engine:

- loads or initializes persistent state
- reconciles with Binance
- fetches fresh market data
- generates target weights from the three sleeves
- applies portfolio and risk constraints
- computes rebalance deltas
- executes paper or live trades
- writes updated state atomically
- returns a human-readable report

### Claude Code Role

Claude Code is the orchestration and control layer, not the alpha source.

It provides:

- remote command and monitoring surfaces
- chat-based operational control
- stateful execution environment
- secure gateway and approval workflow
- phone-accessible status and control through messaging channels

In the current workspace, Telegram and Discord are configured as the operator messaging surfaces. The trading engine’s notifier formats startup, rebalance, status, shutdown, and error reports into plain text suitable for relay back through Claude Code.

### Trigger and Monitoring from Phone

From a practical operator perspective, the phone workflow is:

- initialize engine
- request status
- trigger a rebalance
- review the rebalance report
- stop the engine or flatten positions if needed

What the operator sees on mobile:

- current NAV
- daily and total PnL
- executed trades
- open positions
- gross and net exposure
- drawdown
- circuit breaker state
- volatility regime
- warnings and errors

This is exactly the level of control a systematic operator wants while away from the workstation: concise, actionable, and low-friction.

### Scheduling

The engine is designed for Claude Code cron-based daily scheduling, but it is also well-suited to explicit operator-triggered runs. The present workspace shows the command and reporting path clearly, even if the schedule itself is kept intentionally manual until the operator is satisfied with paper behavior.

## Why This Approach Can Earn Money

The investment thesis is not "we built a bot." It is:

- we target structural and behavioral inefficiencies that have persisted in crypto
- we diversify across independent signal families
- we size by risk rather than emotion
- we penalize turnover and liquidity consumption
- we validate out-of-sample, not just in-sample
- we maintain a simple live process that can be monitored and interrupted quickly

In short, the expected return comes from disciplined harvesting of trend, relative strength, and funding dislocations, translated into a constrained portfolio with explicit cost accounting and risk throttles.

## Why Capital Protection Matters Equally

This strategy is designed around the idea that investor capital survives only if the operating system survives.

That is why the design emphasizes:

- constrained leverage
- capped single-name risk
- drawdown circuit breakers
- regime-aware downscaling
- correlation monitoring
- exchange reconciliation
- conservative execution assumptions
- operator visibility from mobile
- a clean separation between research, paper trading, and live deployment

The objective is not merely to make money in favorable conditions. It is to remain governable in unfavorable ones.

## Closing Statement

This is a systematic crypto portfolio built to behave like an institutional process at small scale:

- diversified in signal source
- disciplined in risk
- conservative in implementation
- transparent in monitoring
- simple in operations

The edge is not one magical model. The edge is the combination of sound signal design, explicit portfolio construction, realistic backtesting, and a live operating framework that is robust enough to be trusted with capital.
