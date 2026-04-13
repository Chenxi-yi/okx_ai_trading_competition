#!/usr/bin/env python3
"""
CLI entry point for walk-forward parameter optimization.

Usage:
    # Optimize portfolio weights (default)
    python run_optimize.py

    # Optimize a specific strategy sleeve
    python run_optimize.py --target trend_momentum

    # Use calmar ratio as objective
    python run_optimize.py --target portfolio_weights --objective calmar

    # Quick test with synthetic data
    python run_optimize.py --quick-test

    # Online recalibration mode
    python run_optimize.py --recalibrate

    # Custom walk-forward windows
    python run_optimize.py --train-months 18 --val-months 6 --step-months 3
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config.profiles import get_profile
from config.settings import (
    INITIAL_CAPITAL,
    TRADING_MODE,
    get_symbols,
)
from data.fetcher import fetch_universe, generate_synthetic_universe
from optimize.param_space import get_param_spaces
from optimize.recalibrate import (
    OnlineRecalibrator,
    print_recalibration_result,
)
from optimize.walk_forward import (
    OBJECTIVE_FUNCS,
    WalkForwardOptimizer,
    print_optimization_result,
)


def parse_args() -> argparse.Namespace:
    target_choices = list(get_param_spaces("daily").keys())
    parser = argparse.ArgumentParser(
        description="Walk-Forward Parameter Optimization",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--target",
        choices=target_choices,
        default="portfolio_weights",
        help="Which parameter space to optimize",
    )
    parser.add_argument(
        "--objective",
        choices=list(OBJECTIVE_FUNCS.keys()),
        default="sharpe",
        help="Metric to maximize (default: sharpe)",
    )
    parser.add_argument("--profile", choices=["daily", "hourly"], default="daily")
    parser.add_argument("--mode", choices=["spot", "futures", "margin"], default=TRADING_MODE)
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--capital", type=float, default=INITIAL_CAPITAL)
    parser.add_argument("--train-months", type=int, default=24, help="Training window (months)")
    parser.add_argument("--val-months", type=int, default=6, help="Validation window (months)")
    parser.add_argument("--step-months", type=int, default=6, help="Step size between folds (months)")
    parser.add_argument("--max-combos", type=int, default=100, help="Max param combinations per fold")
    parser.add_argument(
        "--param-penalty-weight",
        type=float,
        default=0.10,
        help="Penalty weight for parameter choices far from defaults",
    )
    parser.add_argument("--quick-test", action="store_true", help="Use synthetic data, smaller grid")
    parser.add_argument("--recalibrate", action="store_true", help="Run online recalibration instead of WFO")
    parser.add_argument("--max-drift", type=float, default=0.30, help="Max parameter drift for recalibration")
    parser.add_argument(
        "--acceptance-months",
        type=int,
        default=3,
        help="Untouched holdout window used to accept recalibration",
    )
    parser.add_argument(
        "--min-improvement",
        type=float,
        default=0.05,
        help="Minimum holdout objective improvement required to accept recalibration",
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--list-spaces", action="store_true", help="Print parameter spaces and exit")
    parser.add_argument(
        "--engine", choices=["legacy", "unified"], default="legacy",
        help="legacy: original BacktestEngine; unified: full TradingAlgorithm pipeline",
    )
    return parser.parse_args()


def list_spaces() -> None:
    """Print all parameter spaces with their candidate values."""
    for profile_name in ["daily", "hourly"]:
        spaces = get_param_spaces(profile_name)
        print(f"\n{'#'*55}")
        print(f"  PROFILE: {profile_name.upper()}")
        print(f"{'#'*55}")
        for name, space in spaces.items():
            print(f"\n{'='*55}")
            print(f"  {name.upper()}")
            print(f"  Combinations: {space.num_combinations}")
            print(f"{'='*55}")
            print(f"\n  Defaults:")
            for k, v in space.defaults.items():
                print(f"    {k}: {v}")
            print(f"\n  Search Space:")
            for k, v in space.params.items():
                print(f"    {k}: {v}")
    print()


def run_wfo(args: argparse.Namespace) -> None:
    """Run walk-forward optimization."""
    profile = get_profile(args.profile)
    symbols = get_symbols(args.mode)
    start = args.start or profile["backtest_start"]
    end = args.end or profile["backtest_end"]

    if args.quick_test:
        start, end = ("2024-01-01", "2024-03-31") if args.profile == "hourly" else ("2021-01-01", "2023-12-31")
        price_data = generate_synthetic_universe(symbols, start=start, end=end, timeframe=profile["timeframe"])
        max_combos = min(args.max_combos, 20)
        train_months = 2 if args.profile == "hourly" else 12
        val_months = 1 if args.profile == "hourly" else 6
        step_months = 1 if args.profile == "hourly" else min(args.step_months, 6)
    else:
        price_data = fetch_universe(symbols, start=start, end=end, mode=args.mode, timeframe=profile["timeframe"])
        max_combos = args.max_combos
        train_months = args.train_months
        val_months = args.val_months
        step_months = args.step_months

    if not price_data:
        raise RuntimeError("No price data loaded.")

    optimizer = WalkForwardOptimizer(
        target=args.target,
        objective=args.objective,
        mode=args.mode,
        profile_name=args.profile,
        train_months=train_months,
        val_months=val_months,
        step_months=step_months,
        max_combos=max_combos,
        capital=args.capital,
        param_penalty_weight=args.param_penalty_weight,
        use_unified=(args.engine == "unified"),
    )

    result = optimizer.run(price_data, start=start, end=end)
    print_optimization_result(result)


def run_recalibration(args: argparse.Namespace) -> None:
    """Run online recalibration."""
    profile = get_profile(args.profile)
    symbols = get_symbols(args.mode)
    start = args.start or profile["backtest_start"]
    end = args.end or profile["backtest_end"]

    if args.quick_test:
        start, end = ("2024-01-01", "2024-03-31") if args.profile == "hourly" else ("2022-01-01", "2023-12-31")
        price_data = generate_synthetic_universe(symbols, start=start, end=end, timeframe=profile["timeframe"])
    else:
        price_data = fetch_universe(symbols, start=start, end=end, mode=args.mode, timeframe=profile["timeframe"])

    if not price_data:
        raise RuntimeError("No price data loaded.")

    recalibrator = OnlineRecalibrator(
        targets=[args.target],
        objective=args.objective,
        mode=args.mode,
        profile_name=args.profile,
        lookback_months=args.train_months,
        max_combos=min(args.max_combos, 50),
        max_drift_pct=args.max_drift,
        acceptance_months=args.acceptance_months,
        min_improvement=args.min_improvement,
    )

    results = recalibrator.recalibrate(
        price_data=price_data,
        current_date=end,
    )

    print_recalibration_result(results, recalibrator.state)


if __name__ == "__main__":
    args = parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.list_spaces:
        list_spaces()
    elif args.recalibrate:
        run_recalibration(args)
    else:
        run_wfo(args)
