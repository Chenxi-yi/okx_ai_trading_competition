# Strategy Spec Template

```yaml
strategy_id:
hypothesis:
book: core | tactical | speculative
timeframe:
holding_period:
symbols_or_universe:
required_data:
  - ohlcv
required_features:
allowed_regimes:
entry_logic:
exit_logic:
position_sizing:
risk_budget:
expected_failure_modes:
backtest_window:
paper_requirement:
live_enable_default: false
owner_notes:
```

## Promotion Checklist

- [ ] Emits `contracts.Signal` only.
- [ ] Does not import broker, subprocess, or OKX CLI.
- [ ] Uses point-in-time data and registered features.
- [ ] Backtest separates signal timestamp from execution price.
- [ ] Includes fees, slippage, funding, and turnover.
- [ ] Passes account-level risk in backtest.
- [ ] Writes decision journal events in paper mode.
