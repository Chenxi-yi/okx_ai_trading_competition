# Smart-Money Diffusion Research v1

- generated_at: `2026-05-09T09:14:17.556696+00:00`
- as_of: `2026050905` UTC hour
- symbols: `ETH,BTC,SOL,LAB,DOGE,ZEC,RAVE,NOT,XRP,PEPE,BSB,MEGA,TRUMP,LTC,CHIP,TON,CL,HYPE,IP,INTC,BILL,JUP,GALA,OKB,TSLA,RLS,SNDK,FIL,PIPPIN,BASED`
- source: `okx smartmoney signal-trend-by-filter` + local 1h OHLCV cache

## Snapshot

| ccy | rows | latest_dir | latest_traders | latest_weighted_long | latest_net_notional | latest_total_notional |
|---|---:|---|---:|---:|---:|---:|
| BASED | 24 | long | 2 | 100.00% | 54,232 | 54,232 |
| BILL | 24 | long | 3 | 84.32% | 8,865 | 12,915 |
| BSB | 24 | long | 5 | 96.88% | 189,523 | 202,129 |
| BTC | 24 | short | 26 | 21.10% | -12,087,461 | 20,912,858 |
| CHIP | 24 | short | 4 | 37.93% | -2,582 | 10,695 |
| CL | 24 | short | 3 | 46.14% | -52,262 | 677,525 |
| DOGE | 24 | long | 6 | 100.00% | 594,382 | 594,382 |
| ETH | 24 | long | 43 | 86.72% | 43,742,961 | 59,569,458 |
| FIL | 24 | long | 2 | 100.00% | 60,703 | 60,703 |
| GALA | 7 | short | 1 | 0.00% | -5,016 | 5,016 |
| HYPE | 24 | long | 2 | 100.00% | 365,853 | 365,853 |
| INTC | 12 | short | 2 | 42.51% | -17,613 | 117,586 |
| IP | 24 | long | 3 | 100.00% | 189,294 | 189,294 |
| JUP | 5 | long | 2 | 79.11% | 648 | 1,114 |
| LAB | 24 | long | 10 | 76.77% | 240,188 | 448,621 |
| LTC | 24 | long | 3 | 100.00% | 365,382 | 365,382 |
| MEGA | 24 | long | 2 | 100.00% | 13,555 | 13,555 |
| NOT | 24 | long | 4 | 95.52% | 105,877 | 116,293 |
| OKB | 24 | long | 2 | 58.20% | 75,205 | 458,290 |
| PEPE | 24 | short | 5 | 0.00% | -2,317,196 | 2,317,196 |
| PIPPIN | 24 | long | 2 | 100.00% | 33,167 | 33,167 |
| RAVE | 24 | long | 5 | 90.50% | 178,778 | 220,731 |
| RLS | 24 | long | 2 | 88.97% | 21,242 | 27,257 |
| SNDK | 24 | long | 2 | 81.97% | 58,073 | 90,819 |
| SOL | 24 | short | 11 | 42.90% | -569,514 | 4,010,985 |
| TON | 24 | long | 3 | 99.14% | 71,542 | 72,790 |
| TRUMP | 24 | long | 3 | 99.97% | 345,477 | 345,700 |
| TSLA | 24 | long | 2 | 65.80% | 15,821 | 50,079 |
| XRP | 24 | long | 4 | 88.08% | 1,589,623 | 2,087,487 |
| ZEC | 24 | short | 3 | 19.56% | -50,810 | 83,453 |

## Event Study

| event | samples | avg_fwd_1h | avg_fwd_3h | avg_fwd_6h | avg_fwd_12h | avg_fwd_24h |
|---|---:|---:|---:|---:|---:|---:|
| long_diffusion_event | 119 | -0.04% | -0.20% | 0.11% | 0.32% | -3.92% |
| long_exit_event | 75 | -0.16% | -0.82% | -0.44% | -0.95% | -6.51% |
| all_smartmoney_long | 554 | 0.11% | 0.22% | 0.80% | 1.99% | 1.93% |
| all_smartmoney_short | 118 | 0.20% | 0.37% | 0.80% | 2.07% | 4.45% |

## Interpretation

- This is an event-study probe, not a tradable strategy yet.
- Small-count coins must be treated carefully: 1-4 traders can move ratios sharply.
- Next research step: collect a larger universe hourly and test entry/exit rules out-of-sample.
