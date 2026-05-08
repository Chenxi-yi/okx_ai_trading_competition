# OKX USDT Swap Universe Listed Before 2026-03-08

This is the training universe derived from `okx_usdt_swap_ge2m_20260507`.

- Source universe: `engine/data/universe/okx_usdt_swap_ge2m_20260507/manifest.json`
- Source OHLCV run: `rebuild_181_ohlcv_only_5m_15m_1h_4h_1d_20230101_20260507`
- Rule: remove symbols whose first available OHLCV bar is on or after `2026-03-08`
- Kept symbols: 161
- Removed recent listings: 20

Use `manifest.json` or `symbols.txt` as the training input when recent listings should be excluded.
