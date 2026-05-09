# C-Auto 单笔亏损归因：NOT/USDT

- task_id: `c_auto_loss_attribution_NOT_USDT_stop`
- status: researched
- generated_at: `2026-05-09T05:22:29.733789+00:00`

## 交易时间线

| 字段 | 值 |
|---|---:|
| symbol | `NOT/USDT` |
| side | `short` |
| entry_ts | `2026-05-09T04:00:00+00:00` |
| exit_ts | `2026-05-09T04:20:00+00:00` |
| exit_reason | `stop` |
| entry_price_inferred | 0.0006732 |
| stop_price | 0.00069003 |
| target_price | 0.000649638 |
| exit_price | 0.00069003 |
| notional | 117.00U |
| net_return | -2.64% |
| pnl | -3.09U |
| expected_ev | 0.74% |
| p_target | 54.00% |
| leverage | 1.00x |

## 5m K 线验证

2026-05-09T04:20:00+00:00 的 5m K 线触发 `stop`，high=0.0006933, low=0.0006612。

| ts | open | high | low | close |
|---|---:|---:|---:|---:|
| 2026-05-09T04:05:00+00:00 | 0.0006652 | 0.0006657 | 0.0006651 | 0.0006655 |
| 2026-05-09T04:10:00+00:00 | 0.0006691 | 0.0006695 | 0.0006639 | 0.0006639 |
| 2026-05-09T04:15:00+00:00 | 0.0006638 | 0.0006647 | 0.0006598 | 0.0006625 |
| 2026-05-09T04:20:00+00:00 | 0.0006625 | 0.0006933 | 0.0006612 | 0.000678 |

## 历史同类样本

| 切片 | trades | win_rate | avg_net_return | pnl |
|---|---:|---:|---:|---:|
| NOT/USDT short | 13 | 76.92% | 2.82% | 14.37U |
| all short | 2232 | 61.25% | 1.44% | 1760.96U |

## 结论

结论：这笔不是止损缺失，也不是 paper 和生产逻辑不一致；paper 的保护性 stop 已按 5m K 执行。问题更偏向信号/组合筛选：同一 rebalance 同时开了多笔 short，NOT 是排名第 3 的 short 候选。
