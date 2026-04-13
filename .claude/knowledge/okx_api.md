# OKX API & Agent Trade Kit Reference

## Installation
```bash
npm install -g @okx_ai/okx-trade-cli @okx_ai/okx-trade-mcp
```
Requires Node.js ≥ 18. Verify: `okx diagnose --all`

## Authentication
Config file: `~/.okx/config.toml`
```toml
default_profile = "demo"
[demo]
api_key    = "..."
secret_key = "..."
passphrase = "..."
demo       = true
```
Keychain (this project): account=`okx-quant`, services=`okx-quant-api-key` / `okx-quant-secret-key` / `okx-quant-passphrase`
Sync to env: `bash engine/scripts/sync-binance-env-from-keychain.sh` (despite filename, writes OKX keys)

## Profiles
- `--profile demo` → simulated trading (x-simulated-trading: 1 header auto-added)
- `--profile live` → real money — REQUIRES explicit user authorization
- `--json` → returns raw JSON instead of formatted table

## Tag Injection
- CLI tag: `sourceTag = "CLI"` → OKX backend maps to `"agentTradeKit"` in billing
- MCP tag: `sourceTag = "MCP"` → same mapping
- Tag is injected unconditionally on EVERY order — no manual action needed
- Raw ccxt calls do NOT get tagged → orders won't count for competition

## instId Format
| Type | Format | Example |
|---|---|---|
| Perpetual swap | `BASE-QUOTE-SWAP` | `BTC-USDT-SWAP` |
| Spot | `BASE-QUOTE` | `BTC-USDT` |
| Delivery futures | `BASE-QUOTE-YYMMDD` | `BTC-USDT-250627` |
| ccxt → OKX conversion | `BTC/USDT` → `BTC-USDT-SWAP` | `f"{base}-USDT-SWAP"` |

## Contract Size (sz units)
`sz` in swap orders = **contracts**, NOT coins.
| Symbol | Contract Size | Min sz |
|---|---|---|
| BTC-USDT-SWAP | 0.01 BTC | 1 |
| ETH-USDT-SWAP | 0.1 ETH | 1 |
| SOL-USDT-SWAP | 1 SOL | 1 |
| BNB-USDT-SWAP | 0.1 BNB | 1 |
| ADA-USDT-SWAP | 10 ADA | 1 |
| AVAX-USDT-SWAP | 1 AVAX | 1 |

```python
# Coin quantity → contracts
def qty_to_contracts(qty_coins, ct_val):
    return max(1, round(qty_coins / ct_val))
```

## Position Mode — CRITICAL
OKX requires `net` mode for single-direction positions.
- Set once per account: `okx --profile demo account set-position-mode net`
- In `net` mode: `--posSide net` (NOT `long` / `short`)
- Error if wrong: `{"sCode":"51000","sMsg":"Parameter posSide error"}`
- Check current mode: `okx --profile demo account config`

## Core CLI Commands

### Market Data (no auth)
```bash
okx market ticker BTC-USDT-SWAP
okx market candles BTC-USDT-SWAP --bar 1H --limit 100
okx market orderbook BTC-USDT-SWAP --sz 10
okx market funding-rate BTC-USDT-SWAP
okx market open-interest --instType SWAP --instId BTC-USDT-SWAP
okx market indicator rsi BTC-USDT-SWAP --bar 1H --params 14
okx market indicator macd BTC-USDT-SWAP --bar 1H --params 12,26,9
```

### Account (auth required)
```bash
okx --profile demo account balance
okx --profile demo account positions
okx --profile demo account positions-history
okx --profile demo account set-position-mode net
okx --profile demo account config
```

### Swap Orders (auth required)
```bash
# Market open long
okx --profile demo swap place \
  --instId BTC-USDT-SWAP --side buy --ordType market \
  --sz 1 --posSide net --tdMode cross

# Market open short
okx --profile demo swap place \
  --instId BTC-USDT-SWAP --side sell --ordType market \
  --sz 1 --posSide net --tdMode cross

# Market open with TP+SL attached
okx --profile demo swap place \
  --instId BTC-USDT-SWAP --side buy --ordType market \
  --sz 1 --posSide net --tdMode cross \
  --tpTriggerPx 75000 --tpOrdPx -1 \
  --slTriggerPx 60000 --slOrdPx -1

# Close entire position
okx --profile demo swap close \
  --instId BTC-USDT-SWAP --mgnMode cross --posSide net

# Set leverage
okx --profile demo swap set-leverage \
  --instId BTC-USDT-SWAP --lever 3 --mgnMode cross

# Cancel order
okx --profile demo swap cancel --instId BTC-USDT-SWAP --ordId <id>

# Get open orders
okx --profile demo swap orders --instId BTC-USDT-SWAP --status open
```

## Known Pitfalls & Errors

| Code | Symptom | Fix |
|---|---|---|
| `NET_MODE_REQUIRED` | `sCode:51000 posSide error` | Run `set-position-mode net` first |
| `SZ_UNIT_MISMATCH` | Order size too small error | sz is contracts not coins; BTC min=1 (=0.01 BTC) |
| `DEMO_AUTH_FAIL_50101` | `APIKey does not match current environment` | Add `exchange.headers["x-simulated-trading"] = "1"` to ccxt client |
| `CURRENCIES_AUTH_FAIL` | 401 on `fetch_currencies` | Set `exchange.has["fetchCurrencies"] = False` before load_markets |
| `ATK_NOT_FOUND` | `FileNotFoundError: [Errno 2] No such file: 'okx'` | Run `npm install -g @okx_ai/okx-trade-cli` |
| `PROFILE_FLAG_POSITION` | CLI error on profile flag | `--profile` must come BEFORE module name: `okx --profile demo swap place ...` |

## Python Subprocess Pattern
```python
import subprocess, json

def call_atk(args: list, profile="demo") -> dict:
    cmd = ["okx", "--profile", profile, "--json"] + args
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"ATK error: {r.stderr or r.stdout}")
    return json.loads(r.stdout) if r.stdout.strip() else {}
```

## Rate Limits
- Private endpoints: 10 req/s
- Public market data: 20 req/2s
- Batch orders preferred over sequential for multi-symbol operations

## ccxt with OKX (for market data only)
```python
import ccxt
ex = ccxt.okx({
    "apiKey": OKX_API_KEY,
    "secret": OKX_API_SECRET,
    "password": OKX_PASSPHRASE,
    "enableRateLimit": True,
    "options": {"defaultType": "swap"},
})
ex.has["fetchCurrencies"] = False   # prevent 401 on load_markets
if sandbox:
    ex.headers["x-simulated-trading"] = "1"
```
