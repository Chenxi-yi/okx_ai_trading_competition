# Engine Architecture Reference

## Directories
```
engine/
  main.py                         Entry point — all CLI commands
  config/
    settings.py                   Global config, OKX credentials, constants
    profiles.py                   DAILY_PROFILE, HOURLY_PROFILE, get_profile()
    competition_strategies.json   Named competition strategy registry
    strategies.json               Base strategy/preset registry
  competition/
    registry.py                   CompetitionRegistry — loads competition_strategies.json
    backtester.py                 CompetitionBacktester — run/compare backtests
    compare.py                    compare_demo(), compare_backtest(), print_leaderboard()
  strategies/
    base.py                       BaseStrategy
    trend_momentum.py             TrendMomentumStrategy
    cross_sectional_momentum.py   CrossSectionalMomentumStrategy
    funding_carry.py              FundingCarryStrategy
    factory.py                    build_portfolio_strategy()
  backtest/
    runner.py                     BacktestRunner(profile_name, profile_overrides, risk_overrides)
    metrics.py                    compute_all_metrics(), print_metrics()
  execution/
    broker.py                     Broker class — market_buy/sell, get_balance, get_positions
  data/
    fetcher.py                    fetch_ohlcv(), fetch_universe() — OKX via ccxt
    feed_ws.py                    WebSocketPriceCache — live price stream
  risk/
    risk_manager_v2.py            RiskManagerV2 — ATR stops, CB, vol regime, correlation
  logging_/
    structured_logger.py          StructuredLogger — competition logging methods
```

## CLI Commands
```bash
# All commands run from engine/ directory
cd /Users/lucaslee/quant_trade_competition/engine

# Strategy listing
python3 main.py list-strategies

# Competition tools
python3 main.py competition list
python3 main.py competition backtest --strategy elite_flow --start 2025-01-01 --end 2026-03-31
python3 main.py competition backtest-all --start 2025-01-01 --end 2026-03-31
python3 main.py competition compare
python3 main.py competition demo-start --strategy elite_flow
python3 main.py competition demo-status

# Daemon management
python3 main.py start --config '[{"id":"elite_flow","strategy":"combined_portfolio","profile":"daily","capital":300}]'
# Note: capital value is read from competition_strategies.json current_capital — update that file to change deployed amount
python3 main.py status
python3 main.py stop
```

## Key Configuration
```python
# settings.py exports:
OKX_API_KEY, OKX_API_SECRET, OKX_PASSPHRASE  # loaded from env or keychain
LIVE_TRADING = os.environ.get("LIVE_TRADING", "false").lower() == "true"
TRADING_MODE = "futures"

# profiles.py:
get_profile("daily")   # → DAILY_PROFILE dict
get_profile("hourly")  # → HOURLY_PROFILE dict
# profile_overrides applied via _deep_update() in BacktestRunner.run()
```

## Daemon Lifecycle
```
main.py start → _daemonize() → fork → TradingEngine.start() → run_loop()
  ↓
Writes: control/trading.pid, control/startup.txt
Logs:   logs/engine.log (text), logs/engine.jsonl (structured), logs/daemon.log (stdout)

Status read from: logs/summary.json (overwritten every 30s)
State persists:   data/engine_state.json (atomic write with .backup)
Heartbeat:        logs/heartbeat.json (written by heartbeat.py every 2min)

Stop: python3 main.py stop → SIGTERM → graceful shutdown → PID file removed
```

## Log Files
| File | Contents | Updated |
|---|---|---|
| `logs/summary.json` | Latest snapshot of all portfolios | Every 30s |
| `logs/heartbeat.json` | Engine health + enriched portfolio data | Every 2 min |
| `logs/<portfolio_id>.jsonl` | Per-portfolio event stream | Every rebalance |
| `logs/competition/<id>/rebalances.jsonl` | Full detail rebalance log | Every rebalance |
| `logs/competition/<id>/signals.jsonl` | Per-symbol signal breakdown | Every rebalance |
| `logs/competition/<id>/pnl_snapshots.csv` | Periodic NAV snapshots | Every rebalance |
| `logs/competition/<id>/backtest_latest.json` | Latest backtest result | After backtest |

## BacktestRunner Interface
```python
from backtest.runner import BacktestRunner

runner = BacktestRunner(
    profile_name="daily",          # "daily" or "hourly"
    mode="futures",
    initial_capital=registry.current_capital("elite_flow"),  # reads from competition_strategies.json
    profile_overrides={            # deep-merged into profile after loading
        "portfolio_weights": {"trend": 0.50, "cross_sectional": 0.35, "carry": 0.15},
        "portfolio": {"max_gross_leverage": 1.5}
    },
    risk_overrides={               # configures DrawdownCircuitBreakerModel
        "drawdown_cb_1": 0.10,
        "drawdown_cb_2": 0.20
    }
)
results = runner.run(price_data, start="2025-01-01", end="2026-03-31")
# results keys: metrics, nav_series, trades, monthly_pnl, total_fees, total_funding
```

## Data Fetching
```python
from data.fetcher import fetch_universe, fetch_ohlcv

# Auto-refreshes cache if stale (cache end < requested end - 2 days tolerance)
data = fetch_universe(
    ["BTC/USDT", "ETH/USDT", "SOL/USDT"],
    start="2025-01-01", end="2026-03-31",
    mode="futures", timeframe="1d", use_cache=True
)
# Returns Dict[str, pd.DataFrame] — index=UTC timestamp, cols=[open,high,low,close,volume,funding_rate]
```

## CompetitionRegistry Interface
```python
from competition.registry import CompetitionRegistry

reg = CompetitionRegistry()
reg.list_all()              # all strategy dicts
reg.get("elite_flow")      # one strategy dict
reg.to_portfolio_config("elite_flow")  # {"id":"elite_flow","strategy":"combined_portfolio",...}
reg.to_engine_config_json("elite_flow")  # JSON string for --config arg
```
