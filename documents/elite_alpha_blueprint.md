# "Elite Alpha" Adaptive Strategy Blueprint
## Winning the OKX AI Skills Challenge

---

## 1. Strategic Framework: The 14-Day Competition Mandate

In the OKX AI Skills Challenge, the narrow 14-day window (April 9–23) necessitates a radical shift from traditional risk-management frameworks. Because the winning metric is strictly **ROI (Net PnL / Max Cost)**, the architecture must prioritize high-velocity capital turnover and aggressive capture of volatility wicks. A model that over-prioritizes capital preservation will fail to generate the alpha required to secure a top-three finish.

The core mandate is the alignment of model behavior with USDT-Perpetual instruments, where **leverage is the primary engine for ROI amplification**.

### Rules of Engagement

| Parameter | Constraint | Strategic Implication |
|---|---|---|
| Duration | 14-Day Sprint | "Middle-Sprint" aggression; no long-term HODL logic |
| Asset Class | USDT-Perpetuals | Directional momentum + funding rate arb; cross-margin efficiency |
| Winning Metric | ROI Only | Aggressive leverage required to offset the small 300 USDT base |
| Mandatory Tag | `agentTradeKit` | All API payloads must include this tag or be disqualified |
| Execution | AI Only | Manual intervention prohibited; Agent handles all `sz` and `px` logic |

**Game Theory rationale:** A "Maximin" (low-risk) strategy represents a sub-optimal Nash Equilibrium in a winner-takes-all ROI contest. As we enter the "middle-sprint," the Agent must shift from defensive posture to identifying and cooperating with institutional-led momentum bursts.

---

## 2. High-Frequency Signal Generation: The GRU-OFI Hybrid Model

Traditional linear models (ARIMA) are insufficient for non-stationary crypto. The architecture uses **Gated Recurrent Units (GRU)**, which offer similar predictive power to LSTMs with lower computational latency — critical for minute-step execution.

### GRU Performance Baseline
*(Based on Rodrigues & Machado, 2025)*

- **Forecast horizon:** 60 minutes — the "Goldilocks zone" where signal-to-noise ratio is maximized for perpetuals
- **MAPE:** 0.09% | **RMSE:** 77.17
- **Predictive power:** High sensitivity to non-linear price patterns in BTC/ETH liquidity clusters

### Order Flow Imbalance (OFI) Logic

OFI (`e_n`) is calculated by monitoring changes in size (`q`) and price (`P`) at the best bid (`B`) and ask (`A`):

```
e_n = I{P_n^B >= P^B_{n-1}} * q_n^B
    - I{P_n^B <= P^B_{n-1}} * q_{n-1}^B
    - I{P_n^A <= P^A_{n-1}} * q_n^A
    + I{P_n^A >= P^A_{n-1}} * q_{n-1}^A
```

Signals are **Z-score normalised** on a rolling 5-minute window to distinguish routine order book updates from significant institutional pressure.

### Signal Confidence Matrix

| Signal | GRU 60-min Forecast | OFI Z-score |
|---|---|---|
| Strong Bullish | > +0.5% | > 2.0 |
| Moderate Bullish | > 0% | 1.0 – 2.0 |
| Neutral / Wait | Conflicting | -1.0 – 1.0 |
| Moderate Bearish | < 0% | -1.0 – -2.0 |
| Strong Bearish | < -0.5% | < -2.0 |

Raw signals are filtered by **market microstructure filters** — entries only during high-liquidity intervals to avoid gambling on low-volume wicks.

---

## 3. Execution Architecture: Fee Optimization and Order Distribution

### Strategy Kernel & GBM Calibration

Following the Bundi optimal execution strategy, the underlying price is modelled as **Geometric Brownian Motion (GBM)**. This calibration determines the decay rate of the **Strategy Kernel**, which allocates trade volume across the limit order book.

- **Logic:** Volume distributed using exponentially decaying allocation to price levels further from mid-price
- **Objective:** Maximise maker rebates while targeting 60% reduction in execution costs

### OKX API v5 Configuration

- **Account setup:** `POST /api/v5/account/set-position-mode` → `net_type`
- **Margin:** All trades use `tdMode: cross` to maximise utility of the 300 USDT base
- **Tag:** `tag: agentTradeKit` mandatory on every `POST /api/v5/trade/order`
- **V5 field shorthand:** `ccy`, `instId`, `sz`, `px`, `upl`

---

## 4. Liquidation Defense and Heatmap Magnet Integration

In a leveraged environment, **price is a slave to liquidity**. Institutional whales engineer price moves to trigger retail liquidation clusters — acting as "magnets."

### Pro-Architect's Heatmap Checklist (Quadcode)

- **Cluster Persistence:** Only target zones that have persisted for **12hr+**
- **Confluence:** Match clusters with Daily/Weekly S/R and psychological round numbers
- **Gap Check:** Identify liquidity gaps (dark zones) where price moves with zero resistance
- **Absorption Signal:** Monitor **Cumulative Volume Delta (CVD)** — a CVD spike without a price move = whale trap at a liquidation cluster

### Stop-Loss Logic: The "Cold Zone" Placement

- Place stop-losses in **"Cold/Dark zones"** where there is no institutional incentive for stop hunting
- **The 25% Rule:** While 4x leverage allows a theoretical 25% move, "Volatility-Adjusted Maintenance Margin" often ignites liquidation clusters **2–3% earlier** than the nominal liquidation price

---

## 5. Adaptive Logic: The Self-Correcting Competition Engine

### Aggressiveness Coefficient `k`

Static strategies fail in dynamic leaderboards. The Agent recalibrates risk appetite based on hourly standing:

| Regime | Standing | k | Behaviour |
|---|---|---|---|
| Alpha | Leading / Top 3 | 0.5 | Implementation shortfall reduction, capital preservation, maker orders |
| Beta | Mid-Pack | 1.0 | Standard GRU-OFI signal execution, balanced market/limit |
| Gamma | Trailing / Bottom 50% | 2.0 | Increased leverage, "Momentum Accelerator" — targets high-density short-squeeze clusters |

**Size and leverage scaling:**
```
sz    = base_sz    * k
lever = base_lever * k   (capped at 100x)
```

### Self-Correction and Infrastructure Reconciliation

The Agent reconciles state **every 60 seconds** to mitigate WebSocket lag and ADL events:

- **Reconciliation:** Use `tradeId` from OKX private WebSocket to match order fills to position updates
- **Lag Defense:** If `uTime` between orders and positions channels diverges by **>200ms**, pause execution
- **k application:** Recompute `sz` and `lever` at each reconciliation tick

---

## 6. Implementation Blueprint: Skill.md Configuration

### AI Judgment Logic: System Prompt Requirements

1. **Sentiment Ingestion** — Identify high-volatility triggers from news/social feeds
2. **Directional Confluence** — Ensure OFI `e_n` aligns with 60-min GRU forecast
3. **Magnet Check** — Scan the Bitcoin Liquidation Heatmap for whale front-running zones
4. **CVD Check** — Verify price movement is supported by aggressive volume (not a trap)
5. **Stop-Loss Execution** — Calculate "Cold Zone" exit price and submit within initial order payload
6. **Eligibility Check** — Confirm `tag: agentTradeKit` in all outbound JSON

All logic rendered as instructional Markdown. `sz`, `px`, and `lever` derived from adaptive `k` coefficient based on competition countdown and ROI standings.
