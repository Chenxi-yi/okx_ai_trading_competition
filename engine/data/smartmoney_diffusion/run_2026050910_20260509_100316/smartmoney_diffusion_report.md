# Smart-Money Diffusion Research v1

- generated_at: `2026-05-09T10:07:23.022072+00:00`
- as_of: `2026050910` UTC hour
- symbols: `BTC,ETH,SOL,NOT,FIL`
- source: `okx smartmoney signal-trend-by-filter` + local 1h OHLCV cache

## Snapshot

| ccy | rows | latest_dir | latest_traders | latest_weighted_long | latest_net_notional | latest_total_notional |
|---|---:|---|---:|---:|---:|---:|
| NOT | 6 | short | 1 | 0.00% | -5,208 | 5,208 |

## Event Study

| event | samples | avg_fwd_1h | avg_fwd_3h | avg_fwd_6h | avg_fwd_12h | avg_fwd_24h |
|---|---:|---:|---:|---:|---:|---:|
| long_diffusion_event | 0 | -- | -- | -- | -- | -- |
| long_exit_event | 0 | -- | -- | -- | -- | -- |
| short_diffusion_event | 0 | -- | -- | -- | -- | -- |
| short_exit_event | 0 | -- | -- | -- | -- | -- |
| all_smartmoney_long | 0 | -- | -- | -- | -- | -- |
| all_smartmoney_short | 6 | -0.64% | -1.83% | -- | -- | -- |

## Interpretation

- This is an event-study probe, not a tradable strategy yet.
- Small-count coins must be treated carefully: 1-4 traders can move ratios sharply.
- Next research step: collect a larger universe hourly and test entry/exit rules out-of-sample.
