# Monster Lottery Perp Plan — 2026-04-26

## Goal

Build a convex payoff妖币 strategy for a small account:

- Initial account assumption: about 1000 USDT.
- Per idea initial risk budget: 10-20 USDT.
- Accept low win rate if losses are bounded and one large winner can pay for many attempts.
- Trade both directions:
  - Long: early monster expansion before full crowding.
  - Short: post-blowoff breakdown after maker/OI/depth support disappears.
- No live order placement until paper and backtest evidence are acceptable.

## Why This Is Different From `monster_backtest_5m_v1`

The existing long-only monster score backtest continuously allocates NAV and therefore behaves like a noisy momentum system. It lost money over the long sample:

- Final NAV: 545.75 from 1000.00.
- Total return: -45.43%.
- Max drawdown: -73.62%.
- Trade count: 1229.
- Win rate: 34.17%.
- Profit factor: 0.93.

That does not invalidate the monster idea. It says the strategy must be structured as a fixed-risk convex option-like system, not a continuous allocation strategy.

## Required Architecture

### 1. Data Layer

Existing:

- 5m OHLCV cache for 132 OKX swap symbols.
- Funding/OI/long-short historical derivatives snapshots, but OI and long-short history are too short for robust historical training.
- Live ticker/orderbook snapshot in `scripts/refresh_monster_latest.py`.
- Microstructure snapshot collector in `engine/data/microstructure.py`.

Need to add:

- Candidate-only orderbook collector:
  - Reads latest monster watchlist.
  - Samples only top candidates / high-score names, not all 132 symbols.
  - Stores raw flattened book snapshots and derived features.
  - Safe resume; append-only; no deletion of unrelated data.
- Candidate-only OI/funding collector:
  - Same candidate set.
  - Store OI, funding, long/short if available.
  - Use mainly for live/paper and future training because historical coverage is limited.
- Event store:
  - One event table per candidate opportunity with entry signal, live book/OI context, exit reason, realized pnl.

### 2. Feature Layer

Entry features:

- OHLCV monster score from current pipeline.
- Volatility expansion: `rvol_1h/3h/6h`.
- Range expansion: `range_pct_15m/1h/6h`.
- Cross-sectional rank: `cs_rank_ret_6h/24h`.
- Relative volume surge.
- Near-high structure for longs, exhaustion structure for shorts.

Orderbook features:

- `spread_bps`.
- `depth_0p5pct_usd`, `depth_1pct_usd`, `depth_2pct_usd`.
- bid/ask depth imbalance at 0.5%, 1%, 2%.
- top-level imbalance.
- book slope / liquidity wall proxy.
- depth evaporation over 1m/5m/15m.
- spread blowout over 1m/5m/15m.
- microprice vs mid.

Derivatives features:

- OI 5m/15m/1h change.
- price up + OI down divergence.
- price down + OI down post-blowoff unwind.
- OI/volume turnover proxy.
- funding spike / funding sign flip.
- long-short ratio acceleration if available.

Crash-risk / exit features:

- price makes new high but bid depth falls.
- spread expands while price rises.
- OI falls while price rises.
- long upper wick + volume spike.
- bid/ask imbalance flips against position.
- candidate stale-data or orderbook stale-data kill switch.

### 3. Strategy Layer

Two sub-strategies:

#### Monster Lottery Long

Enter when:

- monster score high enough.
- candidate passes liquidity gate.
- recent momentum is strong but not already fully vertical.
- OI is neutral/expanding if available.
- orderbook depth is not evaporating.

Exit when:

- hard stop by price.
- orderbook degradation.
- OI-price divergence.
- time stop.
- staged profit rules.

#### Monster Lottery Short

Enter after blowoff when:

- coin has already pumped aggressively.
- price structure breaks short-term support.
- OI drops or fails to confirm new highs.
- bid depth evaporates / ask pressure thickens.
- spread widens.

Exit when:

- hard stop.
- panic flush target hit.
- OI stabilizes and bid depth replenishes.
- time stop.

### 4. Position/Risk Model

Core principle: every idea has a fixed loss budget.

- `risk_budget_usdt`: 10 or 20.
- Initial margin can be 10-20 USDT, but actual max loss is governed by stop distance and leverage.
- Use leverage only after computing liquidation/stop feasibility.
- If required stop distance implies loss above budget, skip.
- Max concurrent lottery positions: 1-3.
- Daily realized loss cap: e.g. 50-80 USDT.
- Weekly loss cap: e.g. 150-200 USDT.
- Cooldown after consecutive losses.

Roll rules:

- At +30% to +50% position return: reduce enough to recover initial margin/risk.
- At +80% to +100%: trail stop to protect meaningful profit.
- At +150%+: only profit is left at risk.
- Add-ons use realized profit or protected unrealized profit only; never increase original risk budget.

### 5. Backtest Layer

Need a new backtester because ordinary NAV allocation is the wrong objective.

Backtester must support:

- fixed risk budget per trade.
- leverage.
- isolated margin approximation.
- stop-driven position sizing.
- long and short.
- staged exits.
- trailing protection.
- max loss per trade.
- event-level payoff distribution.

Metrics:

- attempts.
- win rate.
- average loss per loser.
- payoff skew.
- best trade.
- worst trade.
- PnL per 100 attempts.
- expected attempts before one large winner.
- ruin probability under 1000 USDT account.
- max consecutive losses.
- survival after 30/50/100 failed attempts.

### 6. Production/Paper Layer

Paper first:

- `scripts/run_monster_paper.py` remains no-order simulation.
- Add lottery paper mode with long/short direction and fixed risk budget.
- Persist:
  - paper state JSON.
  - event ledger JSONL.
  - book/OI snapshots linked by `opportunity_id`.

Live later:

- Must use Agent Trade Kit / `okx swap place`, not raw ccxt.
- Must have explicit user approval before any live/competition order.
- Must check current positions, account balance, orderbook, and stale-data gates before every order.

## Implementation To-Do

1. Run diagnostics on `monster_backtest_5m_v1`.
2. Add `scripts/collect_monster_orderbook.py`.
3. Add `scripts/backtest_monster_lottery.py`.
4. Add derived orderbook feature builder for candidate snapshots.
5. Add OI/funding candidate collector.
6. Extend paper loop with lottery mode.
7. Add launcher status/control for orderbook collector.
8. Run parameter sweeps:
   - risk budget: 10/20.
   - long score thresholds.
   - short blowoff thresholds.
   - stop distance.
   - staged take-profit levels.
9. Document results in `.claude/knowledge/strategies/monster_coin.md`.

## Current Next Step

Start with:

```bash
python3 scripts/analyze_monster_backtest.py --backtest-id monster_backtest_5m_v1
```

Then implement and smoke-test candidate orderbook collection without placing orders.
