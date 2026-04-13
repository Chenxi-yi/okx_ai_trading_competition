# [STRATEGY NAME] (`<id>`)

> Copy this template. Replace every `<placeholder>`. Do not leave any section blank — write "N/A" or "TBD" if genuinely unknown.

---

## Identity
| Field | Value |
|---|---|
| ID | `<id>` |
| Display name | `<Name>` |
| Status | 📋 Planned / 🔬 Testing / ✅ Active / 🚫 Retired |
| Competition capital | `<USDT>` USDT |
| Base profile | `daily` / `hourly` / `custom` |
| Rebalance cadence | every `<N>` hours |

---

## Philosophy
**Edge being exploited:** `<one sentence — what market inefficiency this captures>`

**Works best when:** `<market conditions>`

**Fails when:** `<market conditions>`

**Key risk:** `<primary risk factor>`

---

## Architecture

### Sleeve Composition
| Sleeve | Weight | Class | File |
|---|---|---|---|
| Trend | `<pct>%` | `TrendMomentumStrategy` | `engine/strategies/trend_momentum.py` |
| Cross-Sectional | `<pct>%` | `CrossSectionalMomentumStrategy` | `engine/strategies/cross_sectional_momentum.py` |
| Carry | `<pct>%` | `FundingCarryStrategy` | `engine/strategies/funding_carry.py` |

Combination: `final_weight[sym] = Σ (sleeve_weight[i] × sleeve_signal[i][sym])`

### Signal Pipeline
```
price_data (OHLCV + funding_rate)
  │
  ├─ TrendMomentumStrategy.generate()    → weights[-1]  × <trend_weight>
  ├─ CrossSectionalMomentumStrategy.generate() → weights[-1] × <xs_weight>
  └─ FundingCarryStrategy.generate()    → weights[-1]  × <carry_weight>
         │
         ▼
  SignalCombiner.combine()               → combined target_weights
         │
         ▼
  SignalFilteredPortfolioModel           → filtered targets (min_weight, max_positions)
         │
         ▼
  CompositeRiskModel                     → risk-scaled targets
         │
         ▼
  ExecutionModel / ATK CLI               → orders
```

---

## Signals — Exact Formulas

### Signal 1: [Name]
**Source:** `<ClassName>` in `<file.py>`

**Input:** `closes: pd.DataFrame` (index=timestamp, columns=symbols)

**Formula:**
```
<exact pseudocode or math>
```

**Output:** `signal: pd.DataFrame` same shape as closes, values in [-1, +1]

---

### Signal 2: [Name]
*(repeat block for each signal)*

---

## Parameters

### Profile Overrides (`profile_overrides` in competition_strategies.json)
| Parameter | Path in Profile | Type | Default | Valid Range | Notes |
|---|---|---|---|---|---|
| `<param>` | `portfolio_weights.<sleeve>` | float | `<val>` | 0–1 | Must sum to 1.0 across sleeves |
| `<param>` | `portfolio.max_gross_leverage` | float | `<val>` | 1.0–5.0 | |
| `<param>` | `portfolio.max_net_exposure` | float | `<val>` | 0–2.0 | |
| `<param>` | `portfolio.max_position_pct` | float | `<val>` | 0–1.0 | |
| `<param>` | `portfolio.min_weight_threshold` | float | `<val>` | 0.01–0.10 | signals below this are dropped |

### Risk Overrides (`risk_overrides` in competition_strategies.json)
| Parameter | Key | Type | Default | Notes |
|---|---|---|---|---|
| CB Level 1 | `drawdown_cb_1` | float | `<val>` | Triggers REDUCED scalar (0.5x) |
| CB Level 2 | `drawdown_cb_2` | float | `<val>` | Triggers CASH scalar (0.0x) |

### Strategy-Level Parameters (in settings.py / profiles.py)
| Parameter | Variable | Default | Notes |
|---|---|---|---|
| `<param>` | `<SETTINGS_VAR>` | `<val>` | |

---

## Data Requirements
| Field | Value |
|---|---|
| Timeframe | `<1d / 1h / 1m>` |
| Minimum lookback bars | `<N>` bars (~`<N days>`) |
| Required columns | `open, high, low, close, volume, funding_rate` |
| Symbols | `<list>` |
| Data source | OKX via ccxt (futures mode) |
| Fetch command | `python3 engine/main.py competition backtest --strategy <id>` |

---

## Code Pointers
| Component | Location | Key Method |
|---|---|---|
| Strategy config | `engine/config/competition_strategies.json` | `id: "<id>"` entry |
| Profile | `engine/config/profiles.py` | `get_profile("<base_profile>")` |
| Sleeve 1 | `engine/strategies/trend_momentum.py` | `TrendMomentumStrategy.generate()` |
| Sleeve 2 | `engine/strategies/cross_sectional_momentum.py` | `CrossSectionalMomentumStrategy.generate()` |
| Sleeve 3 | `engine/strategies/funding_carry.py` | `FundingCarryStrategy.generate()` |
| Backtest | `engine/competition/backtester.py` | `CompetitionBacktester.run("<id>")` |
| Registry | `engine/competition/registry.py` | `CompetitionRegistry.get("<id>")` |

---

## Risk Configuration
```json
{
  "risk_overrides": {
    "drawdown_cb_1": <val>,
    "drawdown_cb_2": <val>
  },
  "profile_overrides": {
    "portfolio_weights": {"trend": <val>, "cross_sectional": <val>, "carry": <val>},
    "portfolio": {
      "max_gross_leverage": <val>,
      "max_net_exposure": <val>,
      "max_position_pct": <val>,
      "min_weight_threshold": <val>
    }
  }
}
```

---

## Backtest Results

| Date | Period | Return | Sharpe | Sortino | Max DD | Win% | Trades | Fees |
|---|---|---|---|---|---|---|---|---|
| — | — | — | — | — | — | — | — | — |

**Run a backtest:**
```bash
python3 .claude/tools/backtest_strategy.py <id> --start 2025-01-01 --end 2026-03-31
```

---

## Known Issues / Gotchas
- `<issue>`: `<description and fix>`

---

## Implementation TODO
- [ ] `<task>`
