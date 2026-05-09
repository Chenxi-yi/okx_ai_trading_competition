# C-Auto 同向持仓拥挤过滤评估

- task_id: `c_auto_same_side_concentration_short`
- status: proxy_researched
- generated_at: `2026-05-09T05:22:29.741631+00:00`

## 当前 paper 暴露

| 字段 | 值 |
|---|---:|
| side | `short` |
| current_count | 3 |
| proposed_limit | 2 |
| latest_rebalance_keep_by_score | `OP/USDT, AR/USDT` |
| latest_rebalance_drop_by_score | `NOT/USDT, FIL/USDT` |
| current_gross_notional | 351.00U |

## 历史 proxy：同一 entry_ts 最多 2 笔 `short`

| 切片 | trades | win_rate | avg_net_return | pnl |
|---|---:|---:|---:|---:|
| baseline all | 4200 | 60.60% | 2.86% | 5107.42U |
| limited all | 3084 | 61.32% | 3.51% | 4467.80U |
| baseline short | 2232 | 61.25% | 1.44% | 1760.96U |
| limited short | 1116 | 63.89% | 1.84% | 1121.33U |

- raw_pnl_retention: 87.48%
- avg_net_return_change: 0.66%

## 结论

建议：进入组合级实验，不直接进 paper。这个 proxy 只是在已成交交易表上删单，没有重算资金曲线、候选替补和同时段风险预算。
