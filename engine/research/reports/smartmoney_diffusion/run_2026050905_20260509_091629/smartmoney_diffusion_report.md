# Smart-Money Diffusion Research v1

- generated_at: `2026-05-09T09:16:35.463113+00:00`
- as_of: `2026050905` UTC hour
- symbols: `NOT,FIL,AR,BTC,ETH,SOL`
- source: `okx smartmoney signal-trend-by-filter` + local 1h OHLCV cache

## Snapshot

| ccy | rows | latest_dir | latest_traders | latest_weighted_long | latest_net_notional | latest_total_notional |
|---|---:|---|---:|---:|---:|---:|
| AR | 15 | long | 1 | 100.00% | 983 | 983 |
| BTC | 70 | short | 26 | 21.10% | -12,087,461 | 20,912,858 |
| ETH | 70 | long | 43 | 86.72% | 43,742,961 | 59,569,458 |
| FIL | 65 | long | 2 | 100.00% | 60,703 | 60,703 |
| NOT | 70 | long | 4 | 95.52% | 105,877 | 116,293 |
| SOL | 70 | short | 11 | 42.90% | -569,514 | 4,010,985 |

## Event Study

| event | samples | avg_fwd_1h | avg_fwd_3h | avg_fwd_6h | avg_fwd_12h | avg_fwd_24h |
|---|---:|---:|---:|---:|---:|---:|
| long_diffusion_event | 59 | -0.12% | 0.05% | 0.81% | 0.80% | 1.38% |
| long_exit_event | 52 | 0.01% | 0.27% | 1.10% | 1.10% | 2.72% |
| short_diffusion_event | 38 | -0.05% | -0.14% | -0.45% | -0.67% | -1.11% |
| short_exit_event | 38 | -0.05% | -0.19% | -0.49% | -0.77% | -1.13% |
| all_smartmoney_long | 269 | 0.16% | 0.50% | 0.95% | 2.06% | 3.95% |
| all_smartmoney_short | 91 | -0.01% | -0.04% | -0.16% | 0.10% | -0.31% |

## Interpretation

- This is an event-study probe, not a tradable strategy yet.
- Small-count coins must be treated carefully: 1-4 traders can move ratios sharply.
- Next research step: collect a larger universe hourly and test entry/exit rules out-of-sample.
