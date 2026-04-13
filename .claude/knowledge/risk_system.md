# Risk Management System Reference

## Source
`engine/risk/risk_manager_v2.py` — class `RiskManagerV2`
`engine/core/risk.py` — `CompositeRiskModel`, `DrawdownCircuitBreakerModel`, `VolRegimeModel`, `CorrelationWatchdogModel`

## Combined Risk Scalar
Final position size = `base_size × circuit_breaker_scalar × vol_scalar × correlation_scalar`
- All three are independent; product is applied simultaneously
- Minimum combined scalar in practice: 0.60 × 0.50 × 0.80 = 0.24 (24% of nominal)

## A. Drawdown Circuit Breaker
```
peak_nav → current_nav
drawdown = (peak_nav - current_nav) / peak_nav
```
| Drawdown | State | Scalar | Recovery |
|---|---|---|---|
| < CB1 | NORMAL | 1.0 | — |
| ≥ CB1 (10% daily / 8% hourly) | REDUCED | 0.5 | Recovers if drawdown < 5% |
| ≥ CB2 (20% daily / 15% hourly) | CASH | 0.0 | After 90-day cooldown |

**Effect:** In REDUCED state, all new position targets halved. In CASH state, all positions closed.

## B. Volatility Regime
Rolling annualized volatility over 30 bars (daily) or 720 bars (hourly).
Percentile compared to trailing 252-day vol distribution.

| Regime | Condition | Scalar |
|---|---|---|
| LOW | vol < 20th percentile | 1.25 |
| MEDIUM | 20th–80th percentile | 1.0 |
| HIGH | vol > 80th percentile | 0.80 (daily) / 0.50 (hourly) |

5% hysteresis prevents regime flapping.

## C. Correlation Watchdog
20-day rolling average pairwise correlation across all symbols.
Threshold: 0.85 (daily) / 0.80 (hourly)

**When triggered:** scalar = 0.60 (40% position reduction)
**IMPORTANT:** With 6 crypto assets (BTC/ETH/SOL/BNB/ADA/AVAX), this fires on nearly EVERY rebalance (avg ρ = 0.85–0.95). This is **expected behaviour**, not an error. Log line: `"Correlation watchdog triggered: avg_corr=0.XXX"`.

## D. ATR-Based Dynamic Stop
```python
atr = EMA(TrueRange, period=14)
stop_distance = atr * atr_multiplier  # default 2.0
# HIGH vol regime: multiplier bumped to max(2.0, 3.0)

long_stop = entry_price - stop_distance
short_stop = entry_price + stop_distance
```

## E. VaR / CVaR (Historical, 95%)
```python
returns = nav_series.pct_change().dropna()
VaR_95 = returns.quantile(0.05)    # 5th percentile (loss)
CVaR_95 = returns[returns <= VaR_95].mean()  # expected loss beyond VaR
```
Used for reporting, not position sizing.

## F. Fee & Slippage Model
```
taker_fee  = 0.04%  (4 bps)
maker_fee  = 0.02%  (2 bps)
slippage   = 0.10 × sqrt(order_notional_usd / avg_daily_volume_usd)
```

## Intraday Risk Thresholds
```
INTRADAY_DD_FROM_SESSION_OPEN = 5%   # 5% drop from session-open equity
INTRADAY_DD_FROM_PEAK = 15%          # 15% drop from all-time peak
```

## Portfolio Construction Constraints
```
max_gross_leverage  = 1.5x (daily) / 2.0x (hourly)
max_net_exposure    = 0.50 (daily) / 1.0 (hourly)
max_position_pct    = 25% (daily) / 30% (hourly)
min_weight_threshold = 3% (daily) / 2% (hourly)  # signals below this are dropped
max_positions       = 10 (daily) / 8 (hourly)
```

## Smart Execution Settings
```
spread_threshold_pct = 0.1%   # above this, use limit order
limit_order_timeout  = 30s
twap_depth_threshold = 5%     # order > 5% of book depth → TWAP
twap_slices = 4, interval = 15s
drift_threshold_relative = 25%  # relative drift triggers partial rebalance
drift_debounce_hours = 2
```

## Competition Risk Recommendations
Given 14-day sprint with dynamic capital (seed 300 USDT, topped up based on strategy performance):
- Consider raising correlation threshold to 0.92 to reduce watchdog suppression
- Consider CB1=15%, CB2=30% for more aggressive competition posture
- For GAMMA regime (trailing leaderboard): k=2.0 multiplier on base_sz and leverage
- Leverage safety: with 4x leverage, a 25% move = liquidation. Volatility-adjusted: clusters hit 2-3% earlier.
- Capital scaling: larger `current_capital` improves fee efficiency (fixed fees as smaller % of capital) but does not change risk ratios if leverage is held constant
