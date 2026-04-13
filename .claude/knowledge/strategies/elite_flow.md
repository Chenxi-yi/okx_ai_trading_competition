# Elite Flow (`elite_flow`)

**Elite Alpha successor: multi-level order flow imbalance + crowding model for OKX perpetuals. Built for short-horizon breakout, squeeze, and liquidation-driven continuation trades in a 14-day ROI competition.**

---

## Identity
| Field | Value |
|---|---|
| ID | `elite_flow` |
| Display name | **Elite Flow** |
| Status | 📋 Planned |
| Seed capital | 300 USDT (organiser incentive) |
| Current capital | 300 USDT |
| Base profile | `custom` |
| Rebalance cadence | signal-driven intraday, reconcile every 15s |

---

## Philosophy
**Edge being exploited:** short-horizon perp moves are strongest when microstructure pressure, leverage build-up, and crowd positioning align in the same direction.

**Works best when:** BTC/ETH are moving out of compression, open interest expands, crowding is one-sided, and order-book pressure confirms.

**Fails when:** liquidity is thin, sentiment metrics are stale, or the market chops without follow-through after a local squeeze.

**Key risk:** data and execution complexity; this strategy depends on multiple live feeds and stricter gating than bar-based systems.

---

## Architecture

### Model Components
| Component | Weight | Role | Proposed File |
|---|---|---|---|
| Multi-Level OFI | 45% | entry timing from L2 imbalance | `engine/competition/strategies/elite_flow.py` |
| Crowding | 35% | detect squeeze / flush setup quality | `engine/competition/strategies/elite_flow.py` |
| Momentum-Regime Gate | 20% | direction and trade/no-trade filter | `engine/competition/strategies/elite_flow.py` |

This is a custom strategy, not a `Trend / XS / Carry` portfolio. Final conviction is a weighted composite of the three model blocks plus hard risk gates.

### Signal Pipeline
```text
Live OKX market data
  ├─ order book depth (top N levels)
  ├─ trades / taker flow
  ├─ 1m OHLCV
  ├─ funding rate / premium
  ├─ open interest
  └─ top-account long/short ratio
         │
         ├─ Multi-level OFI engine         → flow_score
         ├─ Crowding engine                → crowd_score
         └─ Momentum / vol regime gate     → regime_score
                  │
                  ▼
         Composite conviction score        → [-1, +1]
                  │
                  ▼
         Trade state machine
           - no trade
           - probe
           - full size
           - de-risk / exit
                  │
                  ▼
         OKX perp execution + 15s reconciliation
```

### Trading Style
- Primary instrument: `BTC-USDT-SWAP`
- Secondary instrument: `ETH-USDT-SWAP` only if BTC feed is healthy and BTC has no active setup
- Max concurrent positions: `1` primary, `2` absolute cap
- Default holding period: `2 min` to `4 h`

---

## Signals — Exact Formulas

### Signal 1: Multi-Level OFI
**Source:** custom `MultiLevelOFICalculator`

**Input:** top `L` bid/ask levels for each book update, default `L = 5`

**Per-level imbalance:**
```python
for level in range(1, L + 1):
    bid_px_t, bid_sz_t = bids_t[level]
    ask_px_t, ask_sz_t = asks_t[level]
    bid_px_p, bid_sz_p = bids_prev[level]
    ask_px_p, ask_sz_p = asks_prev[level]

    e_bid = int(bid_px_t >= bid_px_p) * bid_sz_t - int(bid_px_t <= bid_px_p) * bid_sz_p
    e_ask = int(ask_px_t <= ask_px_p) * ask_sz_t - int(ask_px_t >= ask_px_p) * ask_sz_p
    ofi_level[level] = e_bid - e_ask
```

**Depth-weighted aggregate:**
```python
level_weight[level] = exp(-decay_lambda * (level - 1))   # decay_lambda ~= 0.35
raw_ofi = sum(level_weight[level] * ofi_level[level] for level in 1..L)
ofi_z = zscore(raw_ofi, lookback="5min")
```

**Trade-pressure enhancement:**
```python
taker_delta = taker_buy_volume_30s - taker_sell_volume_30s
taker_z = zscore(taker_delta, lookback="30min")

flow_score = 0.7 * clip(ofi_z / 3.0, -1, 1) + 0.3 * clip(taker_z / 3.0, -1, 1)
```

**Interpretation:** positive `flow_score` means aggressive demand is lifting the book across multiple levels, not just top-of-book noise.

---

### Signal 2: Crowding / Squeeze Score
**Source:** custom `CrowdingModel`

**Inputs:** open interest, funding rate, premium / basis proxy, top-account long/short ratio

**Normalized components:**
```python
oi_chg_15m = pct_change(open_interest, 15min)
oi_z = zscore(oi_chg_15m, lookback="7d")

fund_z = zscore(funding_rate, lookback="30d")
premium_z = zscore(mark_px / index_px - 1.0, lookback="30d")
lsr_z = zscore(log(top_long_short_ratio), lookback="30d")
```

**Directional crowding:**
```python
long_crowded  = mean([ oi_z,  fund_z,  premium_z,  lsr_z])
short_crowded = mean([ oi_z, -fund_z, -premium_z, -lsr_z])
```

**Squeeze / flush setup logic:**
```python
if breakout_up and short_crowded > squeeze_threshold:
    crowd_score = +clip(short_crowded / 3.0, 0, 1)
elif breakout_down and long_crowded > squeeze_threshold:
    crowd_score = -clip(long_crowded / 3.0, 0, 1)
else:
    crowd_score = 0
```

**Interpretation:** the model prefers trading in the direction that hurts the most crowded side once price starts to break.

---

### Signal 3: Momentum-Regime Gate
**Source:** custom `RegimeGate`

**Inputs:** 1-minute closes, realized volatility, range expansion

```python
ret_5m = close / close.shift(5) - 1
ret_30m = close / close.shift(30) - 1
slope_60m = linreg_slope(close[-60:])
rv_60m = annualized_vol(returns[-60:])
rv_rank = percentile_rank(rv_60m, trailing="30d")

trend_score = mean([
    clip(ret_5m / 0.003, -1, 1),
    clip(ret_30m / 0.008, -1, 1),
    clip((slope_60m / close[-1]) / 0.005, -1, 1),
])

if 0.20 <= rv_rank <= 0.90:
    regime_score = trend_score
else:
    regime_score = 0
```

**Interpretation:** skip low-energy chop and panic-vol tails; only trade when directional structure exists in a usable volatility regime.

---

### Signal 4: Composite Conviction
```python
raw_conviction = (
    0.45 * flow_score +
    0.35 * crowd_score +
    0.20 * regime_score
)

if sign(flow_score) != sign(regime_score):
    raw_conviction *= 0.25

if abs(flow_score) < 0.20:
    raw_conviction = 0

conviction = clip(raw_conviction, -1, 1)
```

---

### Signal 5: Position State Machine
```python
if abs(conviction) < 0.25:
    target_state = "FLAT"
elif 0.25 <= abs(conviction) < 0.45:
    target_state = "PROBE"
elif abs(conviction) >= 0.45 and oi_z > 0:
    target_state = "FULL"

target_dir = sign(conviction)
```

**Sizing:**
```python
base_notional = 40    # USDT
size_mult = {"FLAT": 0.0, "PROBE": 0.5, "FULL": 1.0}[target_state]
crowd_boost = min(max(abs(crowd_score) - 0.5, 0), 0.5)
target_notional = base_notional * size_mult * (1.0 + crowd_boost)
```

---

### Signal 6: Exit Logic
```python
exit_now = any([
    sign(flow_score) != position_dir and abs(flow_score) > 0.50,
    pnl_pct <= -stop_loss_pct,
    pnl_pct >= take_profit_pct and abs(flow_score) < 0.25,
    minutes_in_trade >= max_hold_min,
    websocket_lag_ms > lag_ms,
])
```

---

## Parameters

### Core Execution Parameters
| Parameter | Key | Proposed | Notes |
|---|---|---|---|
| Base size | `base_sz_usdt` | `40` | lower than Elite Alpha to allow more selective adds |
| Base leverage | `base_lever` | `2` | use crowding to scale conviction, not raw leverage first |
| Max leverage | `max_lever` | `5` | hard cap |
| Max positions | `max_positions` | `1` | BTC-first |
| Reconcile interval | `reconcile_sec` | `15` | tighter than Elite Alpha |
| Book depth levels | `ofi_levels` | `5` | 3 minimum, 10 max |
| OFI decay lambda | `ofi_decay_lambda` | `0.35` | downweights deeper levels |
| Flow minimum | `flow_min_threshold` | `0.20` | minimum microstructure confirmation before crowding/regime can matter |
| Entry threshold | `entry_threshold` | `0.25` | composite conviction |
| Full-size threshold | `full_threshold` | `0.45` | composite conviction |

### Crowding Parameters
| Parameter | Key | Proposed | Notes |
|---|---|---|---|
| OI lookback | `oi_lookback` | `15m` | short-horizon OI acceleration |
| Squeeze threshold | `squeeze_threshold` | `1.25` | z-score composite; leave stricter than entry gate to avoid crowding noise |
| Funding z lookback | `funding_z_lookback` | `30d` | normalize by long history |
| L/S ratio lookback | `lsr_z_lookback` | `30d` | top-account sentiment normalization |
| Premium z lookback | `premium_z_lookback` | `30d` | basis crowding proxy |

### Risk Parameters
| Parameter | Key | Proposed | Notes |
|---|---|---|---|
| Stop loss | `stop_loss_pct` | `0.020` | 2.0% from entry |
| Take profit | `take_profit_pct` | `0.035` | may trail beyond this |
| Max hold | `max_hold_min` | `240` | 4 hours |
| Daily loss stop | `daily_loss_stop_pct` | `0.05` | no new entries after 5% daily loss |
| Lag threshold | `lag_ms` | `1500` | pause on clearly stale feed |

---

## Data Requirements
| Field | Value |
|---|---|
| Execution timeframe | tick / real-time |
| Signal aggregation | `5s`, `30s`, `1m`, `15m` |
| Minimum warmup | 60 minutes intraday + 30 days normalization history |
| Required streams | order book depth, trades, 1m OHLCV, funding, open interest, top-account long/short ratio |
| Primary symbol | `BTC-USDT-SWAP` |
| Secondary symbol | `ETH-USDT-SWAP` |
| Data source | OKX WebSocket + REST market endpoints |

---

## Code Pointers
| Component | Location | Key Method |
|---|---|---|
| Strategy implementation | `engine/competition/strategies/elite_flow.py` | `EliteFlowStrategy.run()` |
| OFI calculator | `engine/competition/strategies/elite_flow.py` | `MultiLevelOFICalculator.update()` |
| Crowding model | `engine/competition/strategies/elite_flow.py` | `CrowdingModel.score()` |
| Regime gate | `engine/competition/strategies/elite_flow.py` | `RegimeGate.score()` |
| WebSocket feed | `engine/data/feed_ws.py` | `WebSocketPriceCache` |
| Async feed | `engine/data/feed_async.py` | `watch_*` handlers |
| Strategy config | `engine/config/competition_strategies.json` | `"id": "elite_flow"` |
| CLI routing | `engine/main.py` | `_run_custom_strategy()` |

---

## Risk Configuration
```json
{
  "elite_flow_config": {
    "base_sz_usdt": 40,
    "base_lever": 2,
    "max_lever": 5,
    "max_positions": 1,
    "reconcile_sec": 15,
    "ofi_levels": 5,
    "ofi_decay_lambda": 0.35,
    "flow_min_threshold": 0.20,
    "entry_threshold": 0.25,
    "full_threshold": 0.45,
    "squeeze_threshold": 1.25,
    "stop_loss_pct": 0.02,
    "take_profit_pct": 0.035,
    "max_hold_min": 240,
    "daily_loss_stop_pct": 0.05,
    "lag_ms": 1500
  }
}
```

---

## Backtest Results

| Date | Period | Return | Sharpe | Max DD | Notes |
|---|---|---|---|---|---|
| — | pending | — | — | — | Requires custom event replay or demo run |

**Evaluation plan:**
```bash
python3 engine/main.py competition demo-start --strategy elite_flow --foreground
```

---

## Known Issues / Gotchas
- No standard backtest path: the current `BacktestRunner` cannot replay depth, open interest, and crowding data together.
- Sentiment latency matters: top-account long/short ratio may update more slowly than order-book data, so it should be a filter, not a trigger by itself.
- OI expansion can confirm both breakouts and blow-off tops; require direction from flow and price, not OI alone.
- This should stay concentrated. Expanding to many alts likely recreates the same correlation problem documented in the portfolio strategies.

---

## Research Basis
- Multi-level order flow imbalance and deep order flow literature suggest richer depth features outperform top-of-book-only signals for short-horizon forecasting.
- Perpetual futures research supports funding, basis, and open-interest information as useful crowding state variables.
- Exchange sentiment indicators such as top-account long/short ratio are best used as crowding context, not standalone direction predictors.

---

## Implementation TODO
- [ ] Create `engine/competition/strategies/elite_flow.py`
- [ ] Extend feed layer to persist top `5` levels per book update
- [ ] Add OKX open interest polling / stream integration
- [ ] Add top-account long/short ratio fetcher with caching and staleness checks
- [ ] Implement composite conviction and state machine
- [ ] Add `elite_flow` entry to `engine/config/competition_strategies.json`
- [ ] Add custom strategy routing in `engine/main.py`
- [ ] Run demo validation on BTC only before enabling ETH
