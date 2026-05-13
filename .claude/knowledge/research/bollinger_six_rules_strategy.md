# Bollinger Six Rules Research

Last updated: 2026-05-13

## Idea

User idea:

1. 三线齐上，中轨建仓上轨减仓。
2. 三线走平，下轨低吸上轨高抛。
3. 三线齐跌，下轨博弈中轨止盈。
4. 三线开口向上，大行情启动。
5. 三线开口向下，果断离场保本金。
6. 三线持续收口，方向未明静观其变。

Research script:

```bash
python3 scripts/research_bollinger_six_rules.py \
  --timeframe 4h \
  --start 2025-01-01 \
  --max-symbols 80
```

Output root:

`engine/data/research/bollinger_six_rules/`

## Translation To Testable Rules

- Bollinger band: 20-period SMA, 2 standard deviations.
- 三线齐上: upper/middle/lower slopes all positive; test long at middle-band pullback, target upper band.
- 三线走平: all slopes near flat; test lower-band long to middle and upper-band short to middle.
- 三线齐跌: slopes all negative; test lower-band rebound long to middle only.
- 开口向上: bandwidth expands while middle/upper band rise; test close above upper band long.
- 开口向下: treated as no-trade/exit by default; optional short test is available but was not promoted.
- 持续收口: no-trade.

Costs include 5 bps fee per side and 2 bps slippage per side.

## 4h Results

| Run | Rule | Trades | Win | Mean net | Positive months | Recent since 2026-04-01 | Verdict |
|---|---|---:|---:|---:|---:|---|---|
| `bollinger_4h_trend_long` | 三线齐上，中轨建仓上轨减仓 | 563 | 47.07% | -1.081% | 29.41% | 61 trades, 67.21% win, +0.683% mean | Recent-only, not robust |
| `bollinger_4h_range_revert` | 三线走平，下轨低吸上轨高抛 | 1774 | 43.24% | -0.219% | 17.65% | 273 trades, 54.58% win, +0.030% mean | Weak |
| `bollinger_4h_down_rebound` | 三线齐跌，下轨博弈中轨止盈 | 1397 | 33.93% | -0.295% | 29.41% | 72 trades, 48.61% win, +0.554% mean | Avoid |
| `bollinger_4h_expansion_long` | 三线开口向上，大行情启动 | 2561 | 57.01% | -0.197% | 35.29% | 422 trades, 55.45% win, +0.038% mean | Weak after costs/tail |
| `bollinger_4h_trend_long_atr_fast` | 三线齐上 with ATR stop/shorter hold | 1281 | 46.84% | -0.707% | 41.18% | 154 trades, 63.64% win, +0.276% mean | Recent-only |
| `bollinger_4h_expansion_long_atr_fast` | 开口向上 with ATR stop/shorter hold | 3096 | 53.68% | -0.188% | 29.41% | 530 trades, 49.06% win, -0.294% mean | Not useful |

## Daily Result

| Run | Rule | Trades | Win | Mean net | Positive months | Recent since 2026-04-01 | Verdict |
|---|---|---:|---:|---:|---:|---|---|
| `bollinger_1d_expansion_long_atr` | 日线开口向上 | 281 | 69.04% | +0.186% | 30.77% | 80 trades, 65.00% win, +0.396% mean | Marginal |

## Interpretation

First-principles view:

- The six-rule framework is sensible as a regime classifier, not as a direct entry system.
- The most useful information is regime separation: trend, range, expansion, contraction.
- Directly trading each sentence creates too many low-quality trades and gets hurt by fee/slippage plus tail losses.

Main findings:

- 4h "三线齐上" recently works, but fails badly across the full history.
- 4h "开口向上" has high win rate but poor tail/risk; median is positive while mean is negative, which means large losers dominate.
- "三线走平" range trading does not overcome costs.
- "三线齐跌，下轨博弈" is structurally dangerous and should not be used for autonomous long entries.
- Daily "开口向上" is the only marginally positive standalone variant, but the edge is thin and month consistency is poor.

Recommended use:

- Do not add as an independent paper sleeve yet.
- Add Bollinger states as gates/features:
  - `bb_4h_three_up`: can boost long candidates only when other momentum signals agree.
  - `bb_4h_mouth_open_up`: expansion regime flag, not standalone entry.
  - `bb_squeeze`: no-trade / reduce confidence.
  - `bb_mouth_open_down`: reduce long confidence and force tighter exits.
- Avoid direct lower-band knife catching in downtrends.
