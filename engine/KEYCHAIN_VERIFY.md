# OKX API Key Configuration

Credentials are stored in `~/.okx/config.toml` — the same file the Agent Trade Kit CLI reads.
No keychain, no sync scripts.

## Config file location
`~/.okx/config.toml`

## Format
```toml
default_profile = "demo"

[profiles.demo]
api_key    = "your-api-key"
secret_key = "your-secret-key"
passphrase = "your-passphrase"
demo       = true
```

## Verify keys loaded by engine

```bash
cd /Users/lucaslee/quant_trade_competition/engine && python3 -c "
from config.settings import OKX_API_KEY, OKX_API_SECRET, OKX_PASSPHRASE, OKX_API_KEY_SOURCE, OKX_API_SECRET_SOURCE, OKX_PASSPHRASE_SOURCE
print(f'API key:    {bool(OKX_API_KEY)} (source: {OKX_API_KEY_SOURCE})')
print(f'Secret:     {bool(OKX_API_SECRET)} (source: {OKX_API_SECRET_SOURCE})')
print(f'Passphrase: {bool(OKX_PASSPHRASE)} (source: {OKX_PASSPHRASE_SOURCE})')
"
```

## Verify ATK CLI works

```bash
okx --profile demo diagnose --cli
```

Expected: `auth=true  demo=true  auth_api=200  result=PASS`

## Priority order
1. Environment variables (`OKX_API_KEY`, `OKX_SECRET_KEY`, `OKX_PASSPHRASE`)
2. `~/.okx/config.toml` (default)
