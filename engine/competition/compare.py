"""
competition/compare.py
======================
Comparison views for competition strategies — both backtest and live demo.

  compare_backtest()  → reads saved backtest_latest.json for all strategies
  compare_demo()      → reads logs/summary.json for live demo performance
  print_demo_status() → formatted live comparison table
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.settings import BASE_DIR
from competition.registry import CompetitionRegistry

logger = logging.getLogger(__name__)

LOGS_DIR      = BASE_DIR / "logs"
COMP_LOGS_DIR = LOGS_DIR / "competition"


# ---------------------------------------------------------------------------
# Backtest comparison (reads saved JSON results)
# ---------------------------------------------------------------------------

def compare_backtest(registry: Optional[CompetitionRegistry] = None) -> None:
    """
    Load the latest saved backtest result for every registered strategy
    and print a side-by-side comparison.
    """
    from competition.backtester import CompetitionBacktester

    reg = registry or CompetitionRegistry()
    results: Dict[str, Dict[str, Any]] = {}

    for s in reg.list_all():
        sid = s["id"]
        r = CompetitionBacktester.load_latest(sid)
        if r is None:
            print(f"  ⚠  No saved backtest for '{sid}'. Run: python3 main.py competition backtest --strategy {sid}")
            results[sid] = {"error": "no_backtest_saved", "strategy_id": sid, "strategy_name": s["name"]}
        else:
            results[sid] = r

    if results:
        CompetitionBacktester.print_comparison(results)


# ---------------------------------------------------------------------------
# Live demo comparison (reads summary.json)
# ---------------------------------------------------------------------------

def compare_demo(registry: Optional[CompetitionRegistry] = None) -> None:
    """
    Read logs/summary.json and display performance of all competition
    strategy demo runs side-by-side.
    """
    summary_path = LOGS_DIR / "summary.json"
    if not summary_path.exists():
        print("No demo data found. Start demo runs first:")
        print("  python3 main.py competition demo-start --strategy <id>")
        return

    with open(summary_path) as f:
        summary = json.load(f)

    reg = registry or CompetitionRegistry()
    competition_ids = set(reg.ids())
    portfolios = summary.get("portfolios", {})

    # Filter to only competition strategy portfolios
    comp_portfolios = {k: v for k, v in portfolios.items() if k in competition_ids}

    if not comp_portfolios:
        print("No competition strategies are currently running in the demo.")
        print("All active portfolios:", list(portfolios.keys()))
        print("\nStart a demo run with: python3 main.py competition demo-start --strategy <id>")
        return

    updated = summary.get("updated_at", "?")
    print()
    print("=" * 75)
    print(f"  COMPETITION DEMO STATUS — {updated}")
    print("=" * 75)

    # Header
    w = 22
    print(
        f"  {'Strategy':<{w}}  {'NAV':>8}  {'PnL':>9}  {'Return':>7}  "
        f"{'Drawdown':>9}  {'Realized':>9}  {'Positions':>9}  {'CB / Vol'}"
    )
    print("  " + "─" * 73)

    rows = []
    for sid, snap in comp_portfolios.items():
        rows.append((sid, snap))
    rows.sort(key=lambda x: x[1].get("pnl_pct", 0), reverse=True)

    for sid, snap in rows:
        nav       = snap.get("nav", 0)
        capital   = snap.get("capital", 0)
        pnl       = snap.get("pnl", 0)
        pnl_pct   = snap.get("pnl_pct", 0)
        rpnl      = snap.get("realized_pnl", 0)
        dd        = snap.get("drawdown_pct", 0)
        n_pos     = snap.get("n_positions", 0)
        risk      = snap.get("risk", {})
        cb        = risk.get("cb", "?")
        vol       = risk.get("vol", "?")
        strategy  = snap.get("strategy_id", sid)

        sign = "+" if pnl >= 0 else ""
        print(
            f"  {sid:<{w}}  ${nav:>7,.2f}  {sign}${pnl:>7,.2f}  {sign}{pnl_pct:>5.2f}%  "
            f"{dd:>+8.1f}%  {'+' if rpnl>=0 else ''}${rpnl:>7,.2f}  {n_pos:>9d}  {cb}/{vol}"
        )

    print("  " + "─" * 73)
    total_nav  = sum(v.get("nav", 0) for v in comp_portfolios.values())
    total_pnl  = sum(v.get("pnl", 0) for v in comp_portfolios.values())
    total_cap  = sum(v.get("capital", 0) for v in comp_portfolios.values())
    total_pct  = (total_pnl / total_cap * 100) if total_cap > 0 else 0
    sign = "+" if total_pnl >= 0 else ""
    print(
        f"  {'TOTAL':<{w}}  ${total_nav:>7,.2f}  {sign}${total_pnl:>7,.2f}  {sign}{total_pct:>5.2f}%"
    )
    print("=" * 75)
    print()

    # Per-strategy position detail
    for sid, snap in rows:
        positions = snap.get("positions", {})
        if not positions:
            continue
        print(f"  [{sid}] Positions:")
        for sym, pos in sorted(positions.items()):
            upnl = pos.get("upnl", 0)
            print(
                f"    {sym:12s}  {pos.get('side','?'):5s}  "
                f"qty={pos.get('qty',0):+.4f}  "
                f"entry={pos.get('entry',0):,.2f}  "
                f"mark={pos.get('mark',0):,.2f}  "
                f"${pos.get('notional',0):>8,.2f}  "
                f"uPnL=${upnl:+,.2f}"
            )
        print()


# ---------------------------------------------------------------------------
# Leaderboard — ranked by return (competition scoring metric)
# ---------------------------------------------------------------------------

def print_leaderboard(registry: Optional[CompetitionRegistry] = None) -> None:
    """
    Print both saved backtest results and live demo status as a combined
    leaderboard view. Useful for the daily "which strategy is winning" check.
    """
    print("\n" + "=" * 65)
    print("  STRATEGY LEADERBOARD")
    print("=" * 65)
    print("\n── LIVE DEMO ──")
    compare_demo(registry)
    print("\n── BACKTEST ARCHIVE ──")
    compare_backtest(registry)
