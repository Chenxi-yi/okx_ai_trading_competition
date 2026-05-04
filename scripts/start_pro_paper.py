#!/usr/bin/env python3
"""Start a professional pipeline paper run from cached OHLCV data."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
ENGINE_DIR = ROOT / "engine"
sys.path.insert(0, str(ENGINE_DIR))

from contracts import InstrumentSpec
from runtime import OHLCVMarketProvider, PaperRunner, PaperRunnerConfig, PaperScheduler, PaperSchedulerConfig, StrategyLoader


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run Strategy Office strategy through the professional paper pipeline")
    p.add_argument("--strategy-id", default="core_c_auto_h24_regression_v1")
    p.add_argument("--environment", default="personal", choices=["personal", "demo", "competition"])
    p.add_argument("--symbols", default="BTC/USDT,ETH/USDT,SOL/USDT")
    p.add_argument("--timeframe", default="1h")
    p.add_argument("--initial-nav", type=float, default=10_000.0)
    p.add_argument("--interval-sec", type=float, default=60.0)
    p.add_argument("--max-cycles", type=int, default=0, help="0 means run until stopped")
    p.add_argument("--warmup-bars", type=int, default=720)
    p.add_argument("--status-path", default=None)
    p.add_argument("--scheduler-status-path", default=None)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    symbols = [item.strip() for item in args.symbols.split(",") if item.strip()]
    if not symbols:
        raise SystemExit("at least one symbol is required")

    price_data = {symbol: _load_cached_ohlcv(symbol, args.timeframe) for symbol in symbols}
    price_data = {symbol: df for symbol, df in price_data.items() if not df.empty}
    if len(price_data) < 2:
        raise SystemExit("need at least two cached symbols for c-auto paper run")

    strategy = StrategyLoader().load(args.strategy_id, allowed_statuses=("research", "backtest", "paper", "live"))
    instruments = {symbol: _default_instrument(symbol) for symbol in price_data}
    provider = OHLCVMarketProvider(price_data=price_data, instruments=instruments, freshness_sec=0.0)
    provider.cursor = min(max(0, int(args.warmup_bars)), max(0, len(provider.timestamps) - 1))

    status_path = Path(args.status_path) if args.status_path else ENGINE_DIR / "logs" / "pro_paper" / f"{args.strategy_id}_{args.environment}.json"
    scheduler_status_path = (
        Path(args.scheduler_status_path)
        if args.scheduler_status_path
        else ENGINE_DIR / "logs" / "pro_paper" / f"{args.strategy_id}_{args.environment}_scheduler.json"
    )
    stop_path = ENGINE_DIR / "control" / f"pro_paper_{args.strategy_id}_{args.environment}.stop"
    if stop_path.exists():
        stop_path.unlink()

    runner = PaperRunner(
        strategies=[strategy],
        market_provider=provider,
        instruments=instruments,
        config=PaperRunnerConfig(
            initial_nav_usdt=args.initial_nav,
            journal_dir=ENGINE_DIR / "logs" / "pro_paper_journal" / args.strategy_id,
            status_path=status_path,
        ),
    )
    scheduler = PaperScheduler(
        runner,
        PaperSchedulerConfig(
            interval_sec=args.interval_sec,
            max_cycles=None if args.max_cycles <= 0 else args.max_cycles,
            stop_path=stop_path,
            status_path=scheduler_status_path,
            max_consecutive_errors=5,
        ),
    )
    print(
        json.dumps(
            {
                "started_at": datetime.now(timezone.utc).isoformat(),
                "strategy_id": args.strategy_id,
                "environment": args.environment,
                "symbols": sorted(price_data),
                "timeframe": args.timeframe,
                "status_path": str(status_path.relative_to(ROOT)),
                "scheduler_status_path": str(scheduler_status_path.relative_to(ROOT)),
                "stop_path": str(stop_path.relative_to(ROOT)),
            },
            sort_keys=True,
        )
    )
    final_status = scheduler.run()
    print(json.dumps(final_status, sort_keys=True, default=str))
    return 0


def _load_cached_ohlcv(symbol: str, timeframe: str) -> pd.DataFrame:
    safe = symbol.replace("/", "_")
    base = ENGINE_DIR / "data" / "cache" / f"{safe}_futures_{timeframe}"
    parquet = base.with_suffix(".parquet")
    pickle = base.with_suffix(".pkl")
    if parquet.exists():
        return pd.read_parquet(parquet).sort_index()
    if pickle.exists():
        return pd.read_pickle(pickle).sort_index()
    return pd.DataFrame()


def _default_instrument(symbol: str) -> InstrumentSpec:
    return InstrumentSpec(
        inst_id=symbol,
        symbol=symbol,
        ct_val=1.0,
        lot_sz=1.0,
        min_sz=1.0,
        source="pro_paper_default",
        timestamp=datetime.now(timezone.utc),
    )


if __name__ == "__main__":
    raise SystemExit(main())
