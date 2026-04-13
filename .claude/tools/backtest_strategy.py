#!/usr/bin/env python3
"""
Run a competition strategy backtest and print metrics.
Usage: python3 backtest_strategy.py <strategy_id> [--start DATE] [--end DATE] [--no-cache]
Example: python3 backtest_strategy.py elite_flow --start 2025-01-01 --end 2026-03-31
"""
import argparse, sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
ENGINE_DIR   = PROJECT_ROOT / "engine"
sys.path.insert(0, str(ENGINE_DIR))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("strategy_id", help="Strategy ID from competition_strategies.json")
    p.add_argument("--start",    default="2025-01-01")
    p.add_argument("--end",      default="2026-03-31")
    p.add_argument("--no-cache", action="store_true")
    args = p.parse_args()

    import os
    os.chdir(ENGINE_DIR)

    from competition.backtester import CompetitionBacktester
    from competition.registry import CompetitionRegistry

    reg = CompetitionRegistry()
    if not reg.exists(args.strategy_id):
        print(f"Unknown strategy: {args.strategy_id!r}")
        print(f"Available: {', '.join(reg.ids())}")
        sys.exit(1)

    bt = CompetitionBacktester(registry=reg)
    bt.run(
        args.strategy_id,
        start=args.start,
        end=args.end,
        use_cache=not args.no_cache,
        verbose=True,
    )


if __name__ == "__main__":
    main()
