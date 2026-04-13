# System Specification: Hourly Quantitative Crypto Trading Strategy

## 1. Executive Summary & Architecture
This is a systematic, risk-budgeted crypto portfolio designed to trade continuously on Binance USDT-M perpetual futures [1]. The system operates on a 1-hour frequency, relying on three complementary medium-frequency signal sleeves rather than fragile high-frequency microstructure edges [2]. 

The portfolio translates signal convictions into target weights, allowing the execution layer to act as a portfolio optimizer rather than a binary order router [3].

**Sleeve Capital Budgeting:**
*   Trend / Time-Series Momentum: 50% [3]
*   Cross-Sectional Relative Value: 35% [3]
*   Funding-Aware Carry: 15% [3]

---

## 2. Signal Generation Logic & Exact Math Formulas

### Sleeve 1: Trend / Time-Series Momentum (50% Weight)
**Logic:** Captures directional persistence using three sub-signals averaged together, heavily relying on inverse-volatility scaling so that lower-volatility assets carry more weight [4].
**Mathematical Formulation:**
1.  **Dual Moving Average Crossover (DMAC):** Compares a fast moving average against a slow one, applying a band to act as a hysteresis filter and suppress noise [5]. 
    *   *Hourly Setup:* `Fast_MA` (e.g., 12H) vs `Slow_MA` (e.g., 48H).
    *   *Signal:* `+1` if `Fast_MA > Slow_MA * (1 + band)`, `-1` if `Fast_MA < Slow_MA * (1 - band)`, `0` otherwise.
2.  **Breakout Confirmation:** Compares current price to shifted rolling prior highs and lows to prevent a bar from breaking its own signal threshold simultaneously [5, 6].
    *   *Hourly Setup:* `Rolling_High` and `Rolling_Low` over past $N$ hours, shifted by 1 hour.
    *   *Signal:* `+1` if `Price_t > Shifted_Rolling_High`, `-1` if `Price_t < Shifted_Rolling_Low`.
3.  **Time-Series Momentum:** Averages the directional information across multiple horizons rather than relying on a single lookback [5].
    *   *Hourly Setup:* `Sign(Return_12H) + Sign(Return_24H) + Sign(Return_72H) / 3`.
4.  **Composite & Scaling:** 
    *   `raw_score = (dmac + breakout + momentum) / 3` [4].
    *   `final_trend_score = raw_score / Hourly_Realized_Volatility` [4].

### Sleeve 2: Cross-Sectional Momentum (35% Weight)
**Logic:** Ranks assets by percentile to identify relative strength and weakness, which is more stable than raw score comparisons in noisy markets [7].
**Mathematical Formulation:**
1.  Compute hourly returns across several lookback horizons [7].
2.  Rank assets cross-sectionally by percentile ($0.0$ to $1.0$) on each hour [7].
3.  Average these percentile ranks into a composite score [7].
4.  Apply inverse-volatility scaling before normalizing [8].
5.  *Target Weights:* Go long the top-ranked names, and short the bottom-ranked names (when the regime supports shorting) [7].

### Sleeve 3: Funding-Aware Carry (15% Weight)
**Logic:** Captures the structural carry premium of perpetuals but applies a strict "trend veto" to avoid crowding into mean-reverting traps [8].
**Mathematical Formulation:**
1.  Smooth the funding rates over multiple rolling hourly windows [9].
2.  Check against an absolute imbalance threshold.
3.  *Long Target:* Generate weight if smoothed funding is sufficiently negative AND the medium-term trend is NOT adverse [9].
4.  *Short Target:* Generate weight if smoothed funding is sufficiently positive AND the medium-term trend is NOT adverse [9].

---

## 3. Portfolio Construction & Hard Constraints

The system merges the sleeves into a combined weight matrix and strictly enforces the following rules before execution:
*   **Maximum Gross Leverage:** 1.5x [10]
*   **Maximum Net Exposure:** 50% [10]
*   **Maximum Single-Position Size:** 25% of NAV [10]
*   **Minimum Rebalance Notional:** USD 10 [10] *(CRITICAL: For an hourly frequency, enforcing this minimum threshold is the primary defense against micro-trading fee churn [10, 11]).*

---

## 4. Layered Risk Management Framework

The risk process acts as a layered macro throttle to protect the operating system [12].

1.  **ATR-Based Dynamic Stops:** Adapts to the market state rather than using fixed percentages [13].
    *   *Formula:* `stop_distance = ATR_multiplier * Hourly_ATR` [13].
2.  **Volatility Regime Detection:** Classifies realized portfolio volatility into low, medium, or high [14]. 
    *   *Action:* Applies a modest upsize in low vol, remains neutral in medium vol, and forces a defensive downsize in high vol [14].
3.  **Drawdown Circuit Breakers:** Tracks the portfolio high-water mark [15].
    *   *Action:* First threshold mechanically reduces risk; second threshold completely flattens to cash [15]. Includes a reset/cooldown logic [15].
4.  **Correlation Watchdog:** Measures rolling average pairwise correlation across the active book [14].
    *   *Action:* Mechanically cuts position sizes when cross-asset correlations compress upward to prevent systemic clustering risk [14, 16].
5.  **State Reconciliation:** Before every hourly run, the system must reconcile its local state with the exchange (Binance) positions and cash, treating the exchange as the ultimate source of truth [16].

---

## 5. Execution & Backtesting Guardrails

To prevent quantitative fallacies and survive the transition to a 1-hour frequency, the codebase must enforce these execution realities:
*   **Slippage Penalty:** Must be modeled non-linearly using the square-root impact function: `slippage ~ factor * sqrt(order_size / average_daily_volume)` [17].
*   **Lagged Execution (No Look-Ahead):** Target weights must be shifted by one bar (1 hour) before rebalancing [6]. Today's hour executes on the previous hour's information.