# Strategy Idea: BTC Alt Beta Amplifier

Created: 2026-05-04
Candidate ID: `tactical_btc_alt_beta_amplifier_v0`
Status: idea / waiting research

## Observation

User observation on 2026-05-04:

- BTC stood back above the 80,000 area with only a small positive move.
- Multiple altcoins showed much larger positive moves, around 10%-20%.
- The inverse may also matter: a small BTC drop can become a large altcoin drop.

Local Kit read-only check after upgrade showed BTC-USDT-SWAP around 80,024.6 on
the same session. This is an observation only, not yet statistical evidence.

## Hypothesis

BTC can act as the market-wide impulse trigger. When BTC makes a small but
meaningful directional move, especially through a round number or short-term
breakout level, higher-beta altcoins may amplify the move.

The edge is not "BTC up, buy every alt". The edge should be:

```text
BTC directional impulse
+ altcoin relative strength / weakness
+ liquidity and participation confirmation
+ regime filter
= long/short selected high-beta alts
```

## Strategy Shape

Long side:

- BTC is up mildly but cleanly, for example +0.8% to +2.0% over 1h/4h/24h.
- BTC crosses or holds a psychologically important level, such as 80,000.
- Alt breadth is expanding.
- Candidate alts show relative strength versus BTC and USDT pairs.
- Volume/OI confirms participation.
- Long the strongest alts, not BTC itself.

Short side:

- BTC is down mildly but cleanly, for example -0.8% to -2.0%.
- BTC loses a key level or short-term trend.
- Alt breadth weakens.
- Candidate alts show relative weakness versus BTC.
- Volume/OI confirms sell participation.
- Short the weakest high-beta alts.

## Required Data

Minimum:

- BTC OHLCV: 5m, 15m, 1h
- Alt OHLCV: 5m, 15m, 1h
- Universe snapshots
- Funding rate
- Open interest
- Volume and turnover

Useful later:

- Orderbook depth/spread
- Recent trade imbalance
- Liquidation data if available from external source
- Sector grouping: memes, L1, AI, DeFi, stock tokens, etc.

## Candidate Features

BTC impulse:

- BTC return over 15m/1h/4h/24h
- BTC breakout through round number
- BTC distance from VWAP/EMA
- BTC volatility compression then expansion

Alt amplification:

- alt return minus beta-adjusted BTC return
- rolling beta to BTC
- rolling correlation to BTC
- relative volume spike
- relative OI change
- funding pressure
- prior lag after BTC impulse

Market breadth:

- percentage of universe above short EMA
- percentage of universe outperforming BTC
- top decile minus bottom decile return spread
- number of symbols with volume spike

## Backtest Design

Event study first:

1. Detect BTC small-impulse events.
2. Measure alt returns after 5m, 15m, 30m, 1h, 4h.
3. Split by regime:
   - BTC trend up/down/sideways
   - volatility high/low
   - funding positive/negative
   - alt breadth strong/weak
4. Compare:
   - buy strongest alts
   - buy highest beta alts
   - buy volume breakout alts
   - short weakest alts
   - no-trade baseline

Then strategy backtest:

- Select top N alts by score.
- Cap per-symbol notional.
- Use PositionManager for duplicate signal prevention and exits.
- Use account/instrument/strategy risk gates.
- Use delayed execution, fees, slippage, funding.

## Main Pitfalls

- Survivorship bias from current alt universe.
- Lookahead bias from using same-bar BTC breakout and alt close.
- New listings have short history and artificially low coverage.
- High-beta alts may have poor liquidity and large slippage.
- BTC level crossing can be noisy around round numbers.
- Funding/OI may lag or fail for some instruments.
- Long side and short side may need different filters.

## Research Acceptance Criteria

Do not promote unless:

- Event study shows statistically meaningful post-BTC impulse dispersion.
- Edge survives train/test split by time.
- Edge survives symbol holdout.
- Net results remain positive after realistic fees/slippage/funding.
- Drawdown is acceptable for small accounts.
- Strategy does not rely on one cluster of meme coins only.

## Initial Module Placement

```text
Research Lab:
  event study and feature validation

Strategy Office:
  candidate strategy record after first evidence

Backtest:
  ProBacktestEngine only

Runtime:
  paper only after Strategy Office evidence
```
