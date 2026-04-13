# Competition: OKX AI Skills Internal Elite Challenge

## Identity
- Organiser: OKX internal (OKG Block)
- Window: 2026-04-09 00:00 → 2026-04-23 23:59 (14 days)
- Seed capital: 300 USDT (organiser incentive for early-bird). Capital is dynamic — top up based on strategy optimisation.
- Non-early-bird must self-fund ≥ 300 USDT
- Instrument: USDT-margined perpetual swaps only

## Scoring Formula
```
return_rate = AI_net_pnl / (initial_nav + cumulative_deposits) × 100%
```
- Deposits made during competition are added to denominator (max-cost method) — **they dilute ROI**
- Top-up decision rule: only deposit if `expected_return_on_new_capital × days_remaining > roi_dilution_cost`
- Use `engine/competition/capital_optimizer.py:TopUpAdvisor.evaluate()` to compute this before depositing
- Leaderboard refreshes every hour: realized PnL + unrealized PnL
- Final leaderboard frozen at 2026-04-23 23:59

## Order Eligibility Rules
| Scenario | Counts toward AI PnL? |
|---|---|
| AI opens AND AI closes | ✅ Yes |
| AI opens, manual closes | ✅ Yes |
| Manual opens, AI closes | ❌ No |
| Manual opens, manual closes | ❌ No |

**Recognition method:** closing bill must have `tag = "agentTradeKit"` OR position was first opened by AI.

## Prohibited Actions
- Self-dealing: two accounts trading against each other
- Multi-account price manipulation
- Manual orders claiming AI credit

## Skills Submission
- Deadline: 2026-04-24 23:59 (24h after competition ends)
- Late submission = disqualified from prizes
- Format: 5 sections required
  1. Primary trading prompt (required)
  2. Market judgement logic — how agent identifies open/close timing (required)
  3. Risk controls — stop-loss, position sizing (optional but recommended)
  4. Post-competition review (optional)
  5. Key prompt adjustments discovered during competition (optional)
- Submission is for internal review only, not public

## Prize Structure
| Rank | Prize |
|---|---|
| 1st | 1000 USDT + dinner with Ben + public recognition |
| 2nd | 600 USDT + dinner with Ben + public recognition |
| 3rd | 300 USDT + dinner with Ben + public recognition |

## Registration
- Form: https://okg-block.sg.larksuite.com/share/base/form/shrlg3A5ildmr0OB6d6cgO8Neac
- Required: name, email, sub-account UID, team name, slogan, initial Skills description
- Deadline: 2026-04-08 23:59

## Key Dates
```
2026-04-02  Registration opens
2026-04-08  23:59 Registration deadline, early-bird allocation
2026-04-09  Competition starts ⚡
2026-04-16  Mid-competition leaderboard
2026-04-22  24-hour warning
2026-04-23  23:59 Competition ends 🏁
2026-04-24  23:59 Skills submission deadline
2026-04-25  Award ceremony
```
