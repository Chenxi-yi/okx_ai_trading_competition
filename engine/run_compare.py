#!/usr/bin/env python3
"""
run_compare.py — A-B strategy comparison.

Runs two BacktestRunner configurations side-by-side and prints a
formatted comparison table.

Examples:
    # Daily vs hourly profile
    python3 run_compare.py --quick-test

    # Compare custom date ranges
    python3 run_compare.py --start 2023-01-01 --end 2024-01-01 \\
        --a-profile daily --b-profile hourly

    # Compare legacy vs unified engine on same config
    python3 run_compare.py --quick-test \\
        --a-engine legacy --b-engine unified --a-profile daily --b-profile daily
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Dict, Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config.profiles import get_profile
from config.settings import INITIAL_CAPITAL, TRADING_MODE, get_symbols
from data.fetcher import fetch_universe, generate_synthetic_universe


METRICS_TO_COMPARE = [
    ("total_return_pct",         "Total Return (%)"),
    ("annualised_return_pct",    "Ann. Return (%)"),
    ("annualised_volatility_pct","Ann. Volatility (%)"),
    ("sharpe_ratio",             "Sharpe Ratio"),
    ("sortino_ratio",            "Sortino Ratio"),
    ("max_drawdown_pct",         "Max Drawdown (%)"),
    ("calmar_ratio",             "Calmar Ratio"),
    ("total_fees_usd",           "Total Fees ($)"),
    ("total_slippage_usd",       "Total Slippage ($)"),
    ("num_trades",               "Num Trades"),
    ("win_rate_pct",             "Win Rate (%)"),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="A-B Strategy Comparison")
    parser.add_argument("--a-profile", choices=["daily", "hourly"], default="daily")
    parser.add_argument("--b-profile", choices=["daily", "hourly"], default="hourly")
    parser.add_argument("--a-engine",  choices=["legacy", "unified"], default="unified")
    parser.add_argument("--b-engine",  choices=["legacy", "unified"], default="unified")
    parser.add_argument("--mode", choices=["spot", "futures", "margin"], default=TRADING_MODE)
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--capital", type=float, default=INITIAL_CAPITAL)
    parser.add_argument("--quick-test", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def _run_config(
    engine_name: str,
    profile_name: str,
    price_data: Dict,
    start: str,
    end: str,
    capital: float,
    mode: str,
) -> Dict[str, Any]:
    if engine_name == "unified":
        from backtest.runner import BacktestRunner
        runner = BacktestRunner(profile_name=profile_name, mode=mode, initial_capital=capital)
        return runner.run(price_data, start=start, end=end)
    else:
        from backtest.engine import BacktestEngine
        from strategies.factory import build_portfolio_strategy
        strategy = build_portfolio_strategy(price_data=price_data, mode=mode, profile_name=profile_name)
        engine = BacktestEngine(strategy=strategy, price_data=price_data, mode=mode,
                                initial_capital=capital, profile_name=profile_name)
        return engine.run(start=start, end=end)


def print_comparison(label_a: str, label_b: str, res_a: Dict, res_b: Dict) -> None:
    m_a = res_a.get("metrics", {})
    m_b = res_b.get("metrics", {})
    nav_a = res_a.get("nav_series")
    nav_b = res_b.get("nav_series")

    # Header
    col = 22
    w = col * 3 + 4
    print("\n" + "═" * w)
    print(f"  {'METRIC':<{col}}  {'A: ' + label_a:<{col}}  {'B: ' + label_b:<{col}}")
    print("─" * w)

    for key, display in METRICS_TO_COMPARE:
        val_a = m_a.get(key)
        val_b = m_b.get(key)

        def fmt(v):
            if v is None:
                return "N/A"
            if isinstance(v, float):
                return f"{v:+.2f}" if "pct" in key or "ratio" in key else f"${v:,.2f}" if "usd" in key else f"{v:.2f}"
            return str(v)

        winner = ""
        if val_a is not None and val_b is not None and isinstance(val_a, float):
            higher_is_better = "drawdown" not in key and "fees" not in key and "slippage" not in key
            if higher_is_better:
                winner = " ◀" if val_a > val_b else (" ▶" if val_b > val_a else "")
            else:
                winner = " ◀" if val_a < val_b else (" ▶" if val_b < val_a else "")

        fa, fb = fmt(val_a), fmt(val_b)
        print(f"  {display:<{col}}  {fa:<{col}}  {fb + winner:<{col}}")

    print("─" * w)

    # Summary line
    ret_a = m_a.get("total_return_pct", 0)
    ret_b = m_b.get("total_return_pct", 0)
    winner_label = label_a if ret_a > ret_b else (label_b if ret_b > ret_a else "Tied")
    print(f"  Winner (total return): {winner_label}")
    print("═" * w)

    # Monthly P&L tables
    for label, res in [(label_a, res_a), (label_b, res_b)]:
        monthly = res.get("monthly_pnl")
        if monthly is not None and not monthly.empty:
            print(f"\nMonthly Returns (%) — {label}:")
            print(monthly.to_string())


def run(args: argparse.Namespace) -> None:
    # Determine date range from the profiles
    profile_a = get_profile(args.a_profile)
    profile_b = get_profile(args.b_profile)
    start = args.start or max(profile_a["backtest_start"], profile_b["backtest_start"])
    end   = args.end   or min(profile_a["backtest_end"],   profile_b["backtest_end"])

    if args.quick_test:
        start, end = "2022-01-01", "2022-12-31"

    # Fetch data once — use the static symbol list so it's fast
    symbols = get_symbols(args.mode)
    timeframe = "1d"  # daily as common denominator; hourly profile still works on daily data

    if args.quick_test:
        price_data = generate_synthetic_universe(symbols, start=start, end=end, timeframe=timeframe)
    else:
        price_data = fetch_universe(symbols, start=start, end=end, mode=args.mode, timeframe=timeframe)

    if not price_data:
        raise RuntimeError("No price data loaded.")

    label_a = f"{args.a_engine}/{args.a_profile}"
    label_b = f"{args.b_engine}/{args.b_profile}"

    print(f"\nRunning A: {label_a}  ({start} → {end})")
    res_a = _run_config(args.a_engine, args.a_profile, price_data, start, end, args.capital, args.mode)

    print(f"Running B: {label_b}  ({start} → {end})")
    res_b = _run_config(args.b_engine, args.b_profile, price_data, start, end, args.capital, args.mode)

    print_comparison(label_a, label_b, res_a, res_b)


if __name__ == "__main__":
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    run(args)
