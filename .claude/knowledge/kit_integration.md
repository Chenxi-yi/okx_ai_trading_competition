# OKX Agent Trade Kit Integration

Last updated: 2026-05-04T23:15:00+0800

## Role

OKX Agent Trade Kit is the local trading I/O layer. It should be treated as
hands and sensors, not as the strategy brain.

Our system owns strategy logic, research, parameter management, portfolio
construction, position management, risk approval, accounting, and reconciliation.

Kit owns OKX CLI command execution, market/account probes, order submission,
amend/cancel/close, exchange-side TP/SL and algo orders, and local tool-call
audit.

## Boundary

```text
Strategy / Portfolio / Position / Risk
        |
        v
ExecutionRouter
        |
        v
engine/kit/KitExecutionGateway
        |
        v
OKX Agent Trade Kit CLI (`okx`)
        |
        v
OKX
```

No strategy should call `okx` directly. Runtime OKX CLI calls should go through
`engine/kit/`.

## Implemented

```text
engine/kit/schemas.py              KitCommand, KitResult
engine/kit/client.py               KitClient, audit JSONL, live gate
engine/kit/market_probe.py         ticker/orderbook/candles/funding/OI
engine/kit/account_probe.py        balance/positions/bills/fees/audit
engine/kit/execution_gateway.py    place/close/cancel/leverage/protective stop
engine/kit/supervisor.py           low-token local market/account loop
scripts/kit_supervisor.py          runnable supervisor entrypoint
```

`LiveExecutionRouter` now uses `KitExecutionGateway`.

## Runtime Commands

Read-only public market supervisor:

```bash
python3 scripts/kit_supervisor.py \
  --symbols BTC/USDT,ETH/USDT \
  --no-account \
  --interval-sec 30
```

Market + account supervisor on demo profile:

```bash
python3 scripts/kit_supervisor.py \
  --profile demo \
  --symbols BTC/USDT,ETH/USDT \
  --interval-sec 30
```

Status output:

```text
engine/logs/kit/supervisor_status.json
engine/logs/kit/audit.jsonl
```

## Safety Rules

- Default profile is `demo`.
- Live trade commands are blocked unless both are true:
  - `LIVE_TRADING=true`
  - command is created with `allow_live=True`
- Tests must use read-only market commands or fake runners.
- Do not let Kit make strategy decisions.
- Exchange-side protective orders are allowed only after Position/Risk approval.

## Current Local Version

Current installed CLI after upgrade:

```text
@okx_ai/okx-trade-cli 1.3.3
okx --version: 1.3.3 (e6ad1d1e)
```

## Smart-Money Research Interface

CLI 1.3.3 replaced the older `smartmoney signal/signal-history/overview`
commands with:

```text
traders-by-filter
performance-by-trader
trader-positions
trader-positions-history
trader-orders-history
search-trader
signal-overview-by-filter
signal-overview-by-trader
signal-trend-by-filter
signal-trend-by-trader
```

For signal time series, `asOfTime` uses `yyyyMMddHH` in UTC:

```bash
okx smartmoney signal-trend-by-filter --instCcy NOT --asOfTime 2026050905 --granularity 1h --limit 24 --json
```

The observed fields are suitable for strategy research: `tradersWithPosition`,
`longTraders`, `shortTraders`, `weightedLongRatio`, `weightedShortRatio`,
`netNotionalUsdt`, and `totalNotionalUsdt`.

Diagnostics:

- default profile CLI diagnostics passed.
- MCP package/handshake passed with 154 tools loaded.
- `okx diagnose --all` exits non-zero because no MCP client config is
  registered yet; this is not a CLI trading connectivity failure.
- demo profile diagnostics failed authentication with `Invalid OK-ACCESS-KEY`.
  Demo private-account calls require refreshed demo API keys before use.
