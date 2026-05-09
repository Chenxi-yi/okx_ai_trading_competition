# Smart-Money Diffusion Research v1

- generated_at: `2026-05-09T09:07:45.204096+00:00`
- as_of: `2026050905` UTC hour
- symbols: `NOT,FIL,AR`
- source: `okx smartmoney signal-trend-by-filter` + local 1h OHLCV cache

## Snapshot

| ccy | rows | latest_dir | latest_traders | latest_weighted_long | latest_net_notional | latest_total_notional |
|---|---:|---|---:|---:|---:|---:|
| FIL | 24 | long | 2 | 100.00% | 60,703 | 60,703 |
| NOT | 24 | long | 4 | 95.52% | 105,877 | 116,293 |

## Event Study

| event | samples | avg_fwd_1h | avg_fwd_3h | avg_fwd_6h | avg_fwd_12h | avg_fwd_24h |
|---|---:|---:|---:|---:|---:|---:|
| long_diffusion_event | 9 | 0.11% | -0.50% | 1.27% | 1.47% | -- |
| long_exit_event | 5 | -0.73% | -3.52% | -3.42% | -5.58% | -8.90% |
| all_smartmoney_long | 48 | 0.22% | 0.33% | 1.26% | 3.41% | 3.19% |
| all_smartmoney_short | 0 | -- | -- | -- | -- | -- |

## Interpretation

- This is an event-study probe, not a tradable strategy yet.
- Small-count coins must be treated carefully: 1-4 traders can move ratios sharply.
- Next research step: collect a larger universe hourly and test entry/exit rules out-of-sample.
