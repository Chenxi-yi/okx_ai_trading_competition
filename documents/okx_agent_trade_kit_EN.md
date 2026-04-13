# OKX Agent Trade Kit — Complete Reference

> Version 1.2.7 | GitHub: https://github.com/okx/agent-trade-kit

---

## What Is It?

The OKX Agent Trade Kit is **a Node.js execution layer that places trades on OKX and automatically tags every order with a broker code** that OKX maps to `"agentTradeKit"` in your billing records.

It comes in two packages with the same underlying logic:

| Package | Use case |
|---|---|
| `@okx_ai/okx-trade-cli` | Terminal commands or Python subprocess — **no LLM needed** |
| `@okx_ai/okx-trade-mcp` | MCP server for AI agents (Claude, Cursor, etc.) |

**The tag is injected at the execution layer, unconditionally.** It does not matter how you generated the signal — your own Python algorithm, ML model, or manual decision encoded in code. Call the CLI, get the tag. That's it.

### What it is NOT

- It is **not** a signal generator — it has no built-in trading strategy
- It is **not** an ML model — it does not predict prices
- It does **not** provide liquidation heatmap data or sentiment feeds — those are external tools
- It does **not** require an AI agent — you can use it purely from Python subprocess

---

## Installation

```bash
npm install -g @okx_ai/okx-trade-cli @okx_ai/okx-trade-mcp
```

Requirements: Node.js ≥ 18

---

## Authentication

Create `~/.okx/config.toml`:

```toml
default_profile = "demo"

[demo]
api_key    = "your-demo-api-key"
secret_key = "your-demo-secret"
passphrase = "your-demo-passphrase"
demo       = true

[live]
api_key    = "your-live-api-key"
secret_key = "your-live-secret"
passphrase = "your-live-passphrase"
```

Or use the setup wizard:
```bash
okx config init
```

Verify connectivity:
```bash
okx diagnose --all
```

---

## What Functions Does It Have?

The kit covers **every OKX trading function** across 12 modules and ~107 tools:

| Module | What it does |
|---|---|
| **market** | Prices, candles, orderbook, funding rates, open interest, 20+ indicators (RSI, MACD, BB, EMA…) — **no auth required** |
| **account** | Balance, positions, trade history, fees, transfer between accounts |
| **swap** | Place/cancel/amend perpetual swap orders, set leverage, close positions, TP/SL, batch orders |
| **spot** | Same as swap but for spot markets |
| **futures** | Delivery futures orders |
| **option** | Options orders, greeks |
| **bot.grid** | Create/stop/monitor grid trading bots |
| **bot.dca** | Create/stop/monitor DCA bots |
| **earn** | Simple earn, dual investment, on-chain staking, AutoEarn |

### Built-in indicators (`okx market indicator`)

`ma`, `ema`, `rsi`, `macd`, `bb`, `kdj`, `supertrend`, `halftrend`, `alphatrend`, `stoch-rsi`, `qqe`, and more — directly queryable from the CLI with no extra code.

---

## How to Use It: Option A — Pure Programmatic (No LLM)

This is what we use. Your Python engine generates signals, the CLI executes them.

### Setup check

```bash
okx market ticker BTC-USDT-SWAP           # no auth needed
okx --profile demo account balance         # with auth
```

### Python subprocess pattern

```python
import subprocess
import json

def okx_place_swap(inst_id, side, sz, pos_side, td_mode="cross", profile="demo"):
    """
    Place a perpetual swap order via Agent Trade Kit CLI.
    Tag 'agentTradeKit' is injected automatically.
    """
    cmd = [
        "okx", "--profile", profile, "--json",
        "swap", "place",
        "--instId",   inst_id,    # e.g. "BTC-USDT-SWAP"
        "--side",     side,       # "buy" | "sell"
        "--ordType",  "market",
        "--sz",       str(sz),    # in contracts
        "--posSide",  pos_side,   # "long" | "short"
        "--tdMode",   td_mode,    # "cross" | "isolated"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"Order failed: {result.stderr or result.stdout}")
    return json.loads(result.stdout)

# Example: go long 1 contract BTC
order = okx_place_swap("BTC-USDT-SWAP", "buy", 1, "long")
print(order)
```

### Common CLI commands for trading

```bash
# Check market
okx market ticker BTC-USDT-SWAP
okx market orderbook BTC-USDT-SWAP --sz 10
okx market candles BTC-USDT-SWAP --bar 1H --limit 100
okx market funding-rate BTC-USDT-SWAP
okx market indicator rsi BTC-USDT-SWAP --bar 1H --params 14

# Account
okx --profile demo account balance
okx --profile demo account positions

# Place orders
okx --profile demo swap place --instId BTC-USDT-SWAP --side buy \
    --ordType market --sz 1 --posSide long --tdMode cross

# Place with TP/SL attached
okx --profile demo swap place --instId BTC-USDT-SWAP --side buy \
    --ordType limit --px 65000 --sz 1 --posSide long --tdMode cross \
    --tpTriggerPx 70000 --tpOrdPx -1 \
    --slTriggerPx 62000 --slOrdPx -1

# Close position
okx --profile demo swap close --instId BTC-USDT-SWAP \
    --mgnMode cross --posSide long

# Set leverage
okx --profile demo swap set-leverage --instId BTC-USDT-SWAP \
    --lever 3 --mgnMode cross

# Cancel order
okx --profile demo swap cancel --instId BTC-USDT-SWAP --ordId <orderId>

# Get open orders
okx --profile demo swap orders --instId BTC-USDT-SWAP --status open
```

### instId format

| Type | Format | Example |
|---|---|---|
| Perpetual swap | `BASE-QUOTE-SWAP` | `BTC-USDT-SWAP` |
| Spot | `BASE-QUOTE` | `BTC-USDT` |
| Futures | `BASE-QUOTE-YYMMDD` | `BTC-USDT-250627` |

### sz (contract size)

For perpetual swaps, `sz` is in **contracts**, not coins.
- BTC-USDT-SWAP: 1 contract = 0.01 BTC
- ETH-USDT-SWAP: 1 contract = 0.1 ETH
- SOL-USDT-SWAP: 1 contract = 1 SOL

```python
def coins_to_contracts(exchange, symbol, qty_in_coins):
    """Convert coin quantity to OKX contract count."""
    market = exchange.market(f"{symbol}:USDT")
    ct_val = float(market.get("contractSize", 1))
    return max(1, round(qty_in_coins / ct_val))
```

### Batch orders

```bash
okx --profile demo swap batch --action place --orders '[
  {"instId":"BTC-USDT-SWAP","side":"buy","ordType":"market","sz":"1","posSide":"long","tdMode":"cross"},
  {"instId":"ETH-USDT-SWAP","side":"buy","ordType":"market","sz":"5","posSide":"long","tdMode":"cross"}
]'
```

---

## How to Use It: Option B — With an AI Agent (Skill.md)

This is the intended "AI Skills" mode — you write a Skill.md file that instructs Claude or another LLM when and how to trade. The Agent Trade Kit MCP gives the LLM access to all trading tools.

### Step 1: Register the MCP server with Claude Code

Add to your Claude Desktop config:
```json
{
  "mcpServers": {
    "okx-demo": {
      "command": "npx",
      "args": ["-y", "@okx_ai/okx-trade-mcp", "--profile", "demo"]
    }
  }
}
```

Or auto-register:
```bash
okx setup --client claude-desktop
```

### Step 2: Write a Skill.md (system prompt for the agent)

This is the "strategy as a prompt" approach. The AI reads it, interprets market data, and calls the MCP tools to execute.

```markdown
# My Trading Skill

## Step 1 — Data Collection
- Fetch BTC-USDT-SWAP 1H candles (last 100 bars)
- Fetch RSI(14) on 1H timeframe
- Fetch current funding rate

## Step 2 — Signal Logic
- If RSI < 30 AND funding rate < -0.01%: Strong long signal
- If RSI > 70 AND funding rate > 0.01%: Strong short signal
- Otherwise: Hold / do nothing

## Step 3 — Execution
- Position size: 50 USDT notional
- Leverage: 3x cross margin
- Place market order via swap_place_order
- Attach stop-loss at 3% below entry (slOrdPx = -1 for market SL)

## Step 4 — Risk Controls
- Maximum 1 open position at a time
- If drawdown > 10%: close all positions, stop trading
- Check positions before every new entry
```

### Skill.md vs Pure Code — When to use which?

| Situation | Recommendation |
|---|---|
| You have a quantitative algorithm with precise signal logic | **Pure code → CLI subprocess** |
| You want the AI to interpret qualitative signals (news, sentiment, patterns) | **Skill.md + MCP** |
| You need repeatable, backtestable, deterministic behaviour | **Pure code** |
| You want to submit a "Skills" file for the competition | **Skill.md** (required for competition submission) |
| You want the fastest execution and lowest latency | **Pure code** |

**For this competition: use pure code for execution, write a Skill.md for submission.** The Skill.md is what you submit to the judges. The code is what actually runs.

---

## Key Implementation Notes

1. **Demo vs Live**: Pass `--profile demo` for OKX demo account (simulated trading). Pass `--profile live` for real money.

2. **Position mode**: Set to `net_type` before first trade:
   ```bash
   okx --profile demo account set-position-mode net
   ```

3. **The tag**: You never need to add the tag manually. It is always injected by the CLI/MCP. If you call OKX API directly (via ccxt), the tag is NOT added — orders won't count toward competition score.

4. **Exit codes**: `returncode == 0` means success. Always check even on HTTP 200 — batch operations return 1 if any order fails.

5. **Rate limits**: Private endpoints: 10 req/s. Public market data: 20 req/2s.

6. **OKX demo = simulated account**: Unlike Binance testnet (separate URL), OKX demo uses your real account credentials but marks them as `demo: true` in config. If your keys are demo keys, you don't need to do anything special.

---

## Quick Reference: Our Architecture

```
Your Python signal engine
        │
        ▼
  signal: BUY BTC, 1 contract, 3x leverage
        │
        ▼
subprocess.run(["okx", "--profile", "demo", "swap", "place", ...])
        │
        ▼
  OKX exchange ← order tagged "agentTradeKit" ✓
```

Everything before the subprocess call is yours. The CLI handles authentication, API formatting, rate limiting, and tag injection.
