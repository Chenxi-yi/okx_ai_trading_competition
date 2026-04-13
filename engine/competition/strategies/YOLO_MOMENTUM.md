# YOLO Momentum — Aggressive Leveraged Momentum Strategy

## Overview

**Objective:** Achieve 20% ROI on total invested capital as fast as possible using maximum leverage on a single high-conviction directional bet.

**Capital Policy (Martingale Doubling):**

| Round | Margin Deployed | Cumulative Invested | 20% Target (on cumulative) | Profit Needed |
|-------|----------------|--------------------|-----------------------------|---------------|
| 1     | 50 USDT        | 50 USDT            | 10 USDT                     | 10 USDT       |
| 2     | 100 USDT       | 150 USDT           | 30 USDT                     | 30 USDT       |
| 3     | 200 USDT       | 350 USDT           | 70 USDT                     | 70 USDT       |
| 4     | 400 USDT       | 750 USDT           | 150 USDT                    | 150 USDT      |

Total budget: 1000 USDT. If Round 4 fails, strategy is exhausted.

**Once target is hit: close all positions and stop trading entirely.**

---

## Momentum Model: Multi-Timeframe Trend Confluence (MTTC)

### 1. Contract Selection (Pre-Trade Analysis)

Before each round, dynamically discover **all** liquid USDT-SWAP perps on OKX and pick the best one.

**Universe Filters (applied automatically):**
- Exclude equity/TradFi perps (MSTR, TSLA, AAPL, etc.)
- Exclude stablecoins (USDC, DAI, etc.) and index tokens (BTCDOM, DEFI)
- Exclude coins with 24h volume < $3M (illiquid)
- Exclude coins with bid-ask spread > 0.15% (high entry/exit cost)
- Contract values fetched dynamically for unknown alt coins

This means smaller altcoins with strong momentum ARE included — they often have the best short-term momentum due to lower market cap and higher retail participation.

**Selection Criteria (scored 0-100 per asset):**

| Factor | Weight | Metric |
|--------|--------|--------|
| Trend Strength | 35% | ADX(14) on 1H — higher = stronger trend |
| Momentum Alignment | 25% | Agreement across 5m/15m/1H/4H EMAs (all same direction) |
| Volume Confirmation | 20% | Current volume vs 20-period avg (>1.5x = confirmed) |
| Funding Rate Edge | 10% | Negative funding + long bias = contrarian tailwind |
| Volatility Sweet Spot | 10% | ATR% in 50th-85th percentile (enough movement, not chaos) |

Pick the highest-scoring contract. Direction = consensus of the 4 EMA timeframes.

### 2. Entry Signal — Triple Confirmation Gate

All three must align before entry:

**A. Trend Gate (mandatory)**
- 1H EMA(9) vs EMA(21): Must agree with intended direction
- 4H EMA(9) vs EMA(21): Must agree with intended direction
- If disagreement → NO ENTRY, wait

**B. Momentum Burst Gate (mandatory)**
- RSI(14) on 15m: Long entry requires RSI 55-75 (trending but not overbought)
- RSI(14) on 15m: Short entry requires RSI 25-45 (trending but not oversold)
- MACD(12,26,9) histogram on 15m: Must be expanding in trade direction

**C. Volume Gate (mandatory)**
- Current 15m volume bar > 1.2x of 20-bar average volume
- Confirms institutional participation in the move

### 3. Leverage & Position Sizing

**Leverage Selection:**
- Primary: 50x (balances risk/reward for 20% capital target)
- If ATR% (1H) > 3%: Reduce to 30x (high vol = lower leverage)
- If ATR% (1H) < 1%: Increase to 75x (low vol = need more leverage)

**Position Size:**
- Full margin deployment: Use 90% of round capital as margin
- Reserve 10% for potential averaging (one add-on allowed)

**Example (Round 1, BTC at $80,000, 50x leverage):**
- Margin: 45 USDT (90% of 50)
- Notional: 45 × 50 = 2,250 USDT
- BTC amount: 2,250 / 80,000 = 0.028125 BTC
- Contracts: round(0.028125 / 0.01) = 3 contracts

### 4. Exit Rules — The Critical Edge

#### A. Target Exit (primary)
- Calculate exact P&L needed for 20% of cumulative invested capital
- Set take-profit at that exact price level
- **Auto-close and STOP trading when hit**

#### B. Momentum Reversal Exit (protective)
Close immediately if ANY of these trigger:

1. **EMA Cross Reversal:** 1H EMA(9) crosses EMA(21) against position
2. **RSI Divergence:** Price makes new high/low but RSI doesn't confirm (bearish/bullish divergence on 15m)
3. **Volume Climax:** Single bar volume > 5x average with price reversal candle (long upper/lower wick > 60% of candle body)
4. **MACD Flip:** MACD histogram on 15m crosses zero against position direction

#### C. Time Decay Exit
- If position has been open > 4 hours with < 5% of target profit achieved → close
- Momentum trades should work quickly; lingering = weak signal

#### D. Trailing Stop (after 50% of target achieved)
- Once unrealized P&L reaches 50% of target: activate trailing stop
- Trail distance: 40% of unrealized profit
- Example: Target = 10 USDT, at 5 USDT profit → trail at 3 USDT (close if drops to 2 USDT profit)

#### E. Hard Stop-Loss
- Stop-loss at -60% of deployed margin (allows for high-leverage volatility)
- At 50x: this is roughly a 1.2% adverse price move
- Better to take the stop than get liquidated (saves margin remainder for information)

### 5. Mid-Trade Adjustments

**Add-on Rule (use reserved 10%):**
- If position is +30% of target AND momentum is accelerating (MACD histogram growing)
- Add remaining 10% margin at current price
- Adjust target exit to account for blended entry

**Flip Rule:**
- If stopped out by momentum reversal (not hard stop):
- Wait 2 candles (30 minutes on 15m)
- If reversal signals persist → open opposite direction with same sizing
- This captures V-reversals

### 6. Re-Entry After Liquidation (Martingale Protocol)

1. Wait minimum 1 hour (avoid revenge trading on noise)
2. Re-run contract selection analysis (may pick different contract)
3. Double the margin allocation
4. Recalculate 20% target based on NEW cumulative invested
5. Apply same Triple Confirmation Gate — do NOT rush entry
6. If 3 consecutive losses on same direction → mandatory direction flip

---

## Momentum Reversal Detection Model

### Why Most Momentum Strategies Fail

The #1 killer of leveraged momentum trades is **not** picking the wrong direction — it's **staying in too long after the trend exhausts**. This strategy prioritizes exit quality over entry quality.

### Reversal Signals (ranked by reliability)

| Signal | Timeframe | Reliability | Description |
|--------|-----------|-------------|-------------|
| Volume Climax + Doji | 15m | 85% | Massive volume spike with indecisive candle = exhaustion |
| RSI Divergence | 15m/1H | 80% | Price new extreme but RSI fails to confirm |
| EMA(9)/EMA(21) Cross | 1H | 75% | Lagging but reliable trend change confirmation |
| Funding Rate Extreme | 8H | 70% | Funding > 0.1% = crowded long, < -0.1% = crowded short |
| OI Spike + Price Stall | 1H | 70% | New positions opening but price not moving = trap |
| MACD Histogram Divergence | 15m | 65% | Histogram shrinking while price extends |

### Composite Reversal Score

```
reversal_score = (
    0.30 × volume_climax_signal    +  # 1 if volume > 5x avg + reversal candle
    0.25 × rsi_divergence_signal   +  # 1 if divergence detected
    0.20 × ema_cross_signal        +  # 1 if EMA cross against position
    0.15 × funding_extreme_signal  +  # 1 if |funding| > 0.1%
    0.10 × macd_divergence_signal     # 1 if histogram shrinking 3+ bars
)

if reversal_score >= 0.45:  → CLOSE POSITION IMMEDIATELY
if reversal_score >= 0.30:  → TIGHTEN STOP to breakeven
```

---

## Implementation Notes

### OKX CLI Commands Used

```bash
# Set leverage before entry
okx --profile demo swap set-leverage --instId BTC-USDT-SWAP --lever 50 --mgnMode cross

# Market buy (long)
okx --profile demo swap place --instId BTC-USDT-SWAP --side buy --ordType market --sz 3 --posSide net --tdMode cross

# Market sell (short)
okx --profile demo swap place --instId BTC-USDT-SWAP --side sell --ordType market --sz 3 --posSide net --tdMode cross

# Close position
okx --profile demo swap close --instId BTC-USDT-SWAP --mgnMode cross --posSide net

# Check positions
okx --profile demo account positions

# Market data for analysis
okx market candles BTC-USDT-SWAP --bar 15m --limit 100
okx market candles BTC-USDT-SWAP --bar 1H --limit 100
okx market candles BTC-USDT-SWAP --bar 4H --limit 50
okx market indicator rsi BTC-USDT-SWAP --bar 15m --params 14
okx market indicator macd BTC-USDT-SWAP --bar 15m --params 12,26,9
okx market funding-rate BTC-USDT-SWAP
okx market open-interest --instType SWAP --instId BTC-USDT-SWAP
```

### State Tracking

The strategy maintains a state file at `engine/logs/yolo_momentum_state.json`:
```json
{
  "round": 1,
  "cumulative_invested": 50,
  "current_margin": 50,
  "target_profit": 10,
  "status": "HUNTING|IN_POSITION|TARGET_HIT|LIQUIDATED",
  "position": {
    "instId": "BTC-USDT-SWAP",
    "side": "long",
    "entry_price": 80000,
    "sz": 3,
    "leverage": 50,
    "entry_time": "2026-04-09T00:00:00Z"
  },
  "history": []
}
```

### Risk Acknowledgment

This strategy intentionally accepts:
- High probability of liquidation per round (~40-50%)
- Total loss of round margin on liquidation
- Martingale escalation risk (doubling losses)

The edge comes from:
- High win rate on momentum entries (targeting >55%)
- Aggressive exit on reversal signals (cutting losses before liquidation when possible)
- Asymmetric payoff: 20% target vs partial loss recovery via stops
- Multiple attempts with increasing capital

### Competition ROI Calculation

```
ROI = AI_net_pnl / (initial_nav + cumulative_deposits) × 100%

Example success on Round 1:
  initial_nav = 50 USDT
  cumulative_deposits = 0
  AI_net_pnl = 10 USDT
  ROI = 10 / 50 = 20% ✓

Example success on Round 2 (after Round 1 liquidation):
  initial_nav = 50 USDT (lost)
  cumulative_deposits = 100 USDT (Round 2 deposit)
  AI_net_pnl = -50 + 30 = -20 USDT? 
  
  Wait — ROI denominator = initial_nav + cumulative_deposits = 50 + 100 = 150
  To get 20%: need net_pnl = 30 USDT
  Round 2 must profit 30 + 50 (to cover R1 loss) = 80 USDT? 
  
  Actually: net_pnl = total realized across ALL rounds
  If R1 lost 50, R2 needs to profit 80 to achieve net_pnl = 30
  At 50x leverage on 100 USDT margin: notional = 5000 USDT
  Need 80 / 5000 = 1.6% price move ← very achievable
```

**Revised target per round:**
- Round N target profit = 20% × cumulative_invested + sum(all_prior_losses)
- This is the KEY formula that drives position sizing and exit placement
