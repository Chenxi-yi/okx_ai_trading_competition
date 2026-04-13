#!/usr/bin/env python3
"""
CLI entry point for the quantitative crypto backtesting framework.

Two engines available:
  --engine legacy    Original vectorized BacktestEngine (default).
                     Strategy signals computed once for all dates.
  --engine unified   New bar-by-bar BacktestRunner.
                     Runs the exact same TradingAlgorithm pipeline as live
                     trading — risk rules, filters, and execution are identical.
                     Only SimulatedExecution replaces MarketOrderExecution.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest.engine import BacktestEngine
from config.profiles import get_profile
from config.settings import INITIAL_CAPITAL, TRADING_MODE, get_symbols
from data.fetcher import fetch_universe, generate_synthetic_universe
from strategies.factory import build_portfolio_strategy, build_strategy


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Quantitative Crypto Backtesting Framework")
    parser.add_argument("--strategy", choices=["trend", "cross_sectional", "carry", "portfolio"], default="portfolio")
    parser.add_argument("--profile", choices=["daily", "hourly"], default="daily")
    parser.add_argument("--mode", choices=["spot", "futures", "margin"], default=TRADING_MODE)
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--capital", type=float, default=INITIAL_CAPITAL)
    parser.add_argument("--quick-test", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--no-plot", action="store_true")
    parser.add_argument(
        "--engine", choices=["legacy", "unified"], default="legacy",
        help="legacy: original vectorized engine; unified: bar-by-bar TradingAlgorithm pipeline",
    )
    return parser.parse_args()


def run(args: argparse.Namespace) -> None:
    profile = get_profile(args.profile)
    symbols = get_symbols(args.mode)
    start = args.start or profile["backtest_start"]
    end = args.end or profile["backtest_end"]
    if args.quick_test:
        start, end = ("2024-01-01", "2024-03-31") if args.profile == "hourly" else ("2022-01-01", "2022-12-31")

    price_data = (
        generate_synthetic_universe(symbols, start=start, end=end, timeframe=profile["timeframe"])
        if args.quick_test
        else fetch_universe(symbols, start=start, end=end, mode=args.mode, timeframe=profile["timeframe"])
    )
    if not price_data:
        raise RuntimeError("No price data loaded.")

    if args.engine == "unified":
        from backtest.runner import BacktestRunner
        runner = BacktestRunner(
            profile_name=args.profile,
            mode=args.mode,
            initial_capital=args.capital,
        )
        results = runner.run(price_data, start=start, end=end)
        BacktestRunner.print_results(results)
        return

    # --- Legacy engine ---
    if args.strategy == "portfolio":
        strategy = build_portfolio_strategy(
            price_data=price_data,
            mode=args.mode,
            profile_name=args.profile,
        )
    else:
        strategy = build_strategy(args.strategy, profile_name=args.profile)

    engine = BacktestEngine(
        strategy=strategy,
        price_data=price_data,
        mode=args.mode,
        initial_capital=args.capital,
        profile_name=args.profile,
    )
    results = engine.run(start=start, end=end)
    BacktestEngine.print_results(results)


if __name__ == "__main__":
    args = parse_args()
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    run(args)
