#!/usr/bin/env python3
"""
heartbeat.py — Trading engine health check and enriched status writer.

Runs as a cron job every 2 minutes. Performs:
  1. Checks if the trading daemon PID is alive
  2. Reads logs/summary.json (written by daemon every 30s)
  3. Enriches it with strategy descriptions, direction analysis, top positions
  4. Writes logs/heartbeat.json — the single source of truth for status queries

Claude Code reads heartbeat.json for all status queries. No Python execution needed.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
LOGS_DIR = BASE_DIR / "logs"
CONTROL_DIR = BASE_DIR / "control"
PID_FILE = CONTROL_DIR / "trading.pid"
SUMMARY_FILE = LOGS_DIR / "summary.json"
HEARTBEAT_FILE = LOGS_DIR / "heartbeat.json"

STRATEGY_DESCRIPTIONS = {
    "combined_portfolio": "Combined Portfolio: Trend Momentum (50%) + Cross-Sectional Momentum (35%) + Funding Carry (15%)",
    "trend_momentum": "Trend/Time-Series Momentum: DMA crossover + breakout + momentum signals",
    "cross_sectional_momentum": "Cross-Sectional Momentum: Rank & long top-N, short bottom-N",
    "funding_carry": "Funding-Aware Carry: Long/short based on perpetual funding rates",
}

PROFILE_DESCRIPTIONS = {
    "daily": "Daily rebalance (24h), conservative leverage (1.0x), max 10 positions",
    "hourly": "4-hourly rebalance, aggressive leverage (1.5x), max 12 positions",
}


def is_process_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def analyze_portfolio(snap: dict) -> dict:
    """Enrich a portfolio snapshot with direction analysis and key metrics."""
    positions = snap.get("positions", {})
    nav = snap.get("nav", 0)
    capital = snap.get("capital", 0)

    # Direction analysis
    long_notional = 0.0
    short_notional = 0.0
    long_positions = []
    short_positions = []

    for sym, pos in positions.items():
        notional = abs(pos.get("notional", 0))
        weight = pos.get("weight", 0)
        upnl = pos.get("upnl", 0)
        side = pos.get("side", "flat")

        entry = {
            "symbol": sym,
            "notional": round(notional, 2),
            "weight_pct": round(abs(weight) * 100, 1),
            "upnl": round(upnl, 2),
            "qty": pos.get("qty", 0),
            "entry": pos.get("entry", 0),
            "mark": pos.get("mark", 0),
        }

        if side == "long":
            long_notional += notional
            long_positions.append(entry)
        elif side == "short":
            short_notional += notional
            short_positions.append(entry)

    # Sort by notional descending
    long_positions.sort(key=lambda x: x["notional"], reverse=True)
    short_positions.sort(key=lambda x: x["notional"], reverse=True)

    # Main direction
    total_exposure = long_notional + short_notional
    net_exposure = long_notional - short_notional
    if total_exposure > 0:
        if net_exposure > total_exposure * 0.2:
            direction = "NET LONG"
        elif net_exposure < -total_exposure * 0.2:
            direction = "NET SHORT"
        else:
            direction = "MARKET NEUTRAL"
    else:
        direction = "FLAT"

    # Key metrics
    strategy_id = snap.get("strategy_id", "unknown")
    profile = snap.get("profile", "unknown")
    pnl = snap.get("pnl", 0)
    pnl_pct = snap.get("pnl_pct", 0)
    rpnl = snap.get("realized_pnl", 0)
    upnl_total = snap.get("upnl", 0)
    dd = snap.get("drawdown_pct", 0)
    peak = snap.get("peak_nav", capital)
    max_dd = dd  # current drawdown (max since last peak reset)
    fees = snap.get("total_fees", 0)
    risk = snap.get("risk", {})

    return {
        "portfolio_id": snap.get("portfolio_id", "?"),
        "strategy": STRATEGY_DESCRIPTIONS.get(strategy_id, strategy_id),
        "strategy_id": strategy_id,
        "profile": PROFILE_DESCRIPTIONS.get(profile, profile),
        "profile_id": profile,
        "status": snap.get("status", "unknown"),
        "direction": direction,
        "initial_capital": capital,
        "nav": round(nav, 2),
        "pnl": round(pnl, 2),
        "pnl_pct": round(pnl_pct, 2),
        "realized_pnl": round(rpnl, 2),
        "unrealized_pnl": round(upnl_total, 2),
        "total_fees": round(fees, 2),
        "peak_nav": round(peak, 2),
        "drawdown_pct": round(dd, 2),
        "long_exposure": round(long_notional, 2),
        "short_exposure": round(short_notional, 2),
        "net_exposure": round(net_exposure, 2),
        "gross_exposure": round(total_exposure, 2),
        "n_longs": len(long_positions),
        "n_shorts": len(short_positions),
        "n_positions": len(positions),
        "top_longs": long_positions[:5],   # top 5 by notional
        "top_shorts": short_positions[:5],  # top 5 by notional
        "risk_cb": risk.get("cb", "?"),
        "risk_vol": risk.get("vol", "?"),
        "last_rebalance": snap.get("last_rebalance"),
    }


def build_heartbeat() -> dict:
    """Build the enriched heartbeat status."""
    now = datetime.now(timezone.utc).isoformat()

    # Check daemon health
    daemon_alive = False
    daemon_pid = None
    if PID_FILE.exists():
        try:
            daemon_pid = int(PID_FILE.read_text().strip())
            daemon_alive = is_process_running(daemon_pid)
        except (ValueError, OSError):
            pass

    # Read summary
    summary = None
    summary_age_sec = None
    if SUMMARY_FILE.exists():
        try:
            with open(SUMMARY_FILE, "r") as f:
                summary = json.load(f)
            updated = summary.get("updated_at", "")
            if updated:
                updated_dt = datetime.fromisoformat(updated)
                summary_age_sec = (datetime.now(timezone.utc) - updated_dt).total_seconds()
        except Exception:
            pass

    # Engine status
    if daemon_alive and summary:
        if summary_age_sec and summary_age_sec < 120:
            engine_health = "HEALTHY"
        else:
            engine_health = "STALE"  # daemon alive but not updating
    elif daemon_alive and not summary:
        engine_health = "STARTING"
    elif not daemon_alive and summary:
        engine_health = "STOPPED"
    else:
        engine_health = "NOT_RUNNING"

    # Analyze portfolios
    portfolios_enriched = {}
    if summary:
        for pid, snap in summary.get("portfolios", {}).items():
            portfolios_enriched[pid] = analyze_portfolio(snap)

    heartbeat = {
        "heartbeat_at": now,
        "engine_health": engine_health,
        "daemon_pid": daemon_pid,
        "daemon_alive": daemon_alive,
        "summary_age_sec": round(summary_age_sec, 0) if summary_age_sec else None,
        "portfolios": portfolios_enriched,
        "total_nav": round(sum(p.get("nav", 0) for p in portfolios_enriched.values()), 2),
        "total_capital": round(sum(p.get("initial_capital", 0) for p in portfolios_enriched.values()), 2),
        "total_pnl": round(sum(p.get("pnl", 0) for p in portfolios_enriched.values()), 2),
        "total_pnl_pct": round(
            sum(p.get("pnl", 0) for p in portfolios_enriched.values())
            / max(sum(p.get("initial_capital", 0) for p in portfolios_enriched.values()), 1) * 100,
            2,
        ),
    }

    return heartbeat


def write_heartbeat(heartbeat: dict) -> None:
    """Atomically write heartbeat.json."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = HEARTBEAT_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(heartbeat, f, indent=2, default=str)
    os.rename(tmp, HEARTBEAT_FILE)


def format_heartbeat(hb: dict) -> str:
    """Format heartbeat for human-readable output."""
    lines = [
        "=" * 60,
        f"TRADING ENGINE HEARTBEAT — {hb['heartbeat_at']}",
        f"Health: {hb['engine_health']}  |  PID: {hb.get('daemon_pid', '?')}  |  Alive: {hb['daemon_alive']}",
        "=" * 60,
    ]

    for pid, p in hb.get("portfolios", {}).items():
        lines.append(f"\n{'─' * 60}")
        lines.append(f"[{pid}] {p['strategy']}")
        lines.append(f"  Profile: {p['profile']}")
        lines.append(f"  Direction: {p['direction']}  |  Status: {p['status'].upper()}")
        lines.append(f"  Capital: ${p['initial_capital']:,.2f}  →  NAV: ${p['nav']:,.2f}")
        lines.append(f"  Total PnL: ${p['pnl']:+,.2f} ({p['pnl_pct']:+.2f}%)")
        lines.append(f"  Realized: ${p['realized_pnl']:+,.2f}  |  Unrealized: ${p['unrealized_pnl']:+,.2f}")
        lines.append(f"  Fees: ${p['total_fees']:.2f}  |  Drawdown: {p['drawdown_pct']:.1f}% (peak ${p['peak_nav']:,.2f})")
        lines.append(f"  Exposure: long=${p['long_exposure']:,.2f} short=${p['short_exposure']:,.2f} net=${p['net_exposure']:+,.2f}")
        lines.append(f"  Positions: {p['n_positions']} ({p['n_longs']} long, {p['n_shorts']} short)")
        lines.append(f"  Risk: CB={p['risk_cb']}  Vol={p['risk_vol']}")

        if p["top_longs"]:
            lines.append(f"  Top Longs:")
            for pos in p["top_longs"]:
                lines.append(
                    f"    {pos['symbol']:12s}  ${pos['notional']:>8,.2f}  ({pos['weight_pct']:5.1f}%)  "
                    f"uPnL=${pos['upnl']:+,.2f}"
                )
        if p["top_shorts"]:
            lines.append(f"  Top Shorts:")
            for pos in p["top_shorts"]:
                lines.append(
                    f"    {pos['symbol']:12s}  ${pos['notional']:>8,.2f}  ({pos['weight_pct']:5.1f}%)  "
                    f"uPnL=${pos['upnl']:+,.2f}"
                )
        lines.append(f"  Last Rebalance: {p.get('last_rebalance', 'Never')}")

    lines.append(f"\n{'=' * 60}")
    lines.append(
        f"TOTAL: NAV=${hb['total_nav']:,.2f}  "
        f"PnL=${hb['total_pnl']:+,.2f} ({hb['total_pnl_pct']:+.2f}%)  "
        f"Capital=${hb['total_capital']:,.2f}"
    )
    lines.append("=" * 60)

    return "\n".join(lines)


if __name__ == "__main__":
    heartbeat = build_heartbeat()
    write_heartbeat(heartbeat)

    # Also print human-readable summary
    print(format_heartbeat(heartbeat))
