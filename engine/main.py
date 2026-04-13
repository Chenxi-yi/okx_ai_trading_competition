#!/usr/bin/env python3
"""
main.py — Unified CLI for the quantitative trading engine.

Commands:
  list-strategies   Show available strategies and profiles
  start             Start the trading daemon with portfolio config
  status            Read latest status from logs (no daemon needed)
  stop              Stop the trading daemon gracefully

Usage:
  python3 main.py list-strategies
  python3 main.py start --config '[{"id":"daily","strategy":"combined_portfolio","profile":"daily","capital":5000}]'
  python3 main.py status
  python3 main.py stop
"""

import argparse
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config.settings import BASE_DIR

CONTROL_DIR = BASE_DIR / "control"
PID_FILE = CONTROL_DIR / "trading.pid"
LOGS_DIR = BASE_DIR / "logs"
STRATEGIES_FILE = BASE_DIR / "config" / "strategies.json"


# ---------------------------------------------------------------------------
# list-strategies
# ---------------------------------------------------------------------------

def cmd_list_strategies(args):
    """Print available strategies as formatted text."""
    if not STRATEGIES_FILE.exists():
        print("Error: strategies.json not found")
        return

    with open(STRATEGIES_FILE, "r") as f:
        data = json.load(f)

    strategies = data.get("strategies", [])
    lines = [
        "=" * 55,
        "AVAILABLE STRATEGIES",
        "=" * 55,
    ]
    for i, s in enumerate(strategies, 1):
        profiles = ", ".join(s.get("profiles", []))
        lines.append(f"\n  {i}. {s['name']}")
        lines.append(f"     ID: {s['id']}")
        lines.append(f"     Profiles: {profiles}")
        if s.get("description"):
            lines.append(f"     {s['description']}")

    # Portfolio presets
    presets = data.get("portfolio_presets", {})
    if presets:
        lines.append("")
        lines.append("=" * 55)
        lines.append("PORTFOLIO PRESETS  (use with --preset <name>)")
        lines.append("=" * 55)
        for name, preset in presets.items():
            lines.append(f"\n  {name}")
            lines.append(f"     {preset.get('description', '')}")
            cfg_str = json.dumps(preset["config"])
            lines.append(f"     --config '{cfg_str}'")

    lines.append("")
    lines.append("=" * 55)
    lines.append("To start, use: main.py start --config '<json>'")
    lines.append("Config format: [{\"id\":\"name\",\"strategy\":\"<id>\",\"profile\":\"daily|hourly\",\"capital\":5000}]")
    lines.append("=" * 55)
    print("\n".join(lines))


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------

def cmd_start(args):
    """Start the trading daemon."""
    # Parse portfolio config
    try:
        portfolio_configs = json.loads(args.config)
    except json.JSONDecodeError as e:
        print(f"Error: invalid JSON config: {e}")
        sys.exit(1)

    if not isinstance(portfolio_configs, list) or not portfolio_configs:
        print("Error: config must be a non-empty JSON array")
        sys.exit(1)

    # Validate config entries
    valid_strategies = set()
    if STRATEGIES_FILE.exists():
        with open(STRATEGIES_FILE, "r") as f:
            data = json.load(f)
        valid_strategies = {s["id"] for s in data.get("strategies", [])}

    for cfg in portfolio_configs:
        if "id" not in cfg or "strategy" not in cfg or "capital" not in cfg:
            print(f"Error: each config entry needs 'id', 'strategy', 'capital'. Got: {cfg}")
            sys.exit(1)
        if valid_strategies and cfg["strategy"] not in valid_strategies:
            print(f"Error: unknown strategy '{cfg['strategy']}'. Valid: {sorted(valid_strategies)}")
            sys.exit(1)

    # Check if already running
    if PID_FILE.exists():
        old_pid = int(PID_FILE.read_text().strip())
        if _is_process_running(old_pid):
            print(f"Error: trading engine already running (PID={old_pid}). Stop it first.")
            sys.exit(1)
        PID_FILE.unlink()  # Stale PID file

    # Daemonize: fork and let parent exit
    if not args.foreground:
        _daemonize()

    # Configure logging for the daemon process
    log_file = LOGS_DIR / "engine.log"
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(log_file),
            *([] if not args.foreground else [logging.StreamHandler()]),
        ],
    )

    # Import engine after daemonize (heavy imports)
    from engine.trading_engine import TradingEngine

    paper_mode = getattr(args, "paper", False)
    if paper_mode:
        # Paper trading: fetch real mainnet prices, but no real orders
        logging.getLogger().info("PAPER TRADING MODE — real prices, no real orders")
        engine = TradingEngine(sandbox=False, mode="futures", paper=True)
    else:
        engine = TradingEngine(sandbox=args.sandbox, mode="futures")
    startup_msg = engine.start(portfolio_configs)

    if args.foreground:
        print(startup_msg)

    # Write startup message to a file so the plugin can read it
    startup_file = CONTROL_DIR / "startup.txt"
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    startup_file.write_text(startup_msg)

    # Run the event loop (blocks until stop signal)
    engine.run_loop()


def _daemonize():
    """Fork the process to run as a background daemon."""
    # First fork
    try:
        pid = os.fork()
        if pid > 0:
            # Parent: print PID and exit
            # Wait a moment for child to write PID
            time.sleep(1)
            if PID_FILE.exists():
                child_pid = PID_FILE.read_text().strip()
                print(f"Trading daemon started (PID={child_pid})")
            else:
                print(f"Trading daemon started (PID={pid})")
            print("Use 'python3 main.py status' to check progress.")
            print("Use 'python3 main.py stop' to stop.")
            os._exit(0)
    except OSError as e:
        print(f"Fork failed: {e}")
        sys.exit(1)

    # Decouple from parent
    os.setsid()

    # Second fork (prevent acquiring controlling terminal)
    try:
        pid = os.fork()
        if pid > 0:
            os._exit(0)
    except OSError:
        sys.exit(1)

    # Redirect standard file descriptors
    sys.stdout.flush()
    sys.stderr.flush()
    devnull = open(os.devnull, "r")
    os.dup2(devnull.fileno(), sys.stdin.fileno())

    # Redirect stdout/stderr to log
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_out = open(LOGS_DIR / "daemon.log", "a")
    os.dup2(log_out.fileno(), sys.stdout.fileno())
    os.dup2(log_out.fileno(), sys.stderr.fileno())


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def cmd_status(args):
    """Read and display the latest status from logs/summary.json."""
    from logging_.structured_logger import StructuredLogger

    summary = StructuredLogger.read_summary()
    if not summary:
        print("No trading data available. Engine may not have started yet.")
        return

    updated = summary.get("updated_at", "?")
    engine_status = summary.get("engine_status", "unknown")
    pid = summary.get("pid", "?")
    try:
        os.kill(int(pid), 0)
    except Exception:
        print(f"Status snapshot is stale (pid={pid} is not running).")
        return
    portfolios = summary.get("portfolios", {})

    lines = [
        "=" * 55,
        f"TRADING STATUS — {updated}",
        f"Engine: {engine_status.upper()} (PID={pid})",
        "=" * 55,
    ]

    for pid_name, snap in portfolios.items():
        nav = snap.get("nav", 0)
        capital = snap.get("capital", 0)
        pnl = snap.get("pnl", 0)
        pnl_pct = snap.get("pnl_pct", 0)
        upnl = snap.get("upnl", 0)
        rpnl = snap.get("realized_pnl", 0)
        dd = snap.get("drawdown_pct", 0)
        gross = snap.get("gross_exp", 0)
        net = snap.get("net_exp", 0)
        n_pos = snap.get("n_positions", 0)
        status = snap.get("status", "?")
        strategy = snap.get("strategy_id", "?")
        profile = snap.get("profile", "?")
        last_reb = snap.get("last_rebalance", "Never")
        risk = snap.get("risk", {})

        lines.append(f"\n--- [{pid_name}] {strategy} ({profile}) ---")
        lines.append(f"  Status: {status.upper()}")
        lines.append(f"  NAV: ${nav:,.2f}  (capital: ${capital:,.2f})")
        lines.append(f"  Total PnL: ${pnl:+,.2f} ({pnl_pct:+.2f}%)")
        lines.append(f"  Realized: ${rpnl:+,.2f}  |  Unrealized: ${upnl:+,.2f}")
        lines.append(f"  Drawdown: {dd:.1f}%  |  Fees: ${snap.get('total_fees', 0):.2f}")
        lines.append(f"  Exposure: gross={gross:.1%} net={net:+.1%}")
        lines.append(f"  Risk: CB={risk.get('cb', '?')} Vol={risk.get('vol', '?')}")
        lines.append(f"  Last Rebalance: {last_reb}")

        positions = snap.get("positions", {})
        if positions:
            lines.append(f"  Positions ({n_pos}):")
            for sym, pos in sorted(positions.items()):
                lines.append(
                    f"    {sym:12s}  {pos.get('side','?'):5s}  qty={pos.get('qty',0):+.6f}  "
                    f"entry={pos.get('entry',0):.2f}  mark={pos.get('mark',0):.2f}  "
                    f"${pos.get('notional',0):>10,.2f}  ({pos.get('weight',0):+.1%})  "
                    f"uPnL=${pos.get('upnl',0):+,.2f}"
                )

    lines.append("")
    lines.append("-" * 55)
    lines.append(
        f"TOTAL: NAV=${summary.get('total_nav', 0):,.2f}  "
        f"PnL=${summary.get('total_pnl', 0):+,.2f} ({summary.get('total_pnl_pct', 0):+.2f}%)"
    )
    lines.append("-" * 55)

    print("\n".join(lines))


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------

def cmd_stop(args):
    """Stop the trading daemon gracefully via SIGTERM."""
    if not PID_FILE.exists():
        print("No PID file found. Engine may not be running.")
        # Check summary for last known state
        summary_file = LOGS_DIR / "summary.json"
        if summary_file.exists():
            with open(summary_file, "r") as f:
                summary = json.load(f)
            status = summary.get("engine_status", "unknown")
            print(f"Last known engine status: {status}")
        return

    pid = int(PID_FILE.read_text().strip())

    if not _is_process_running(pid):
        print(f"Process {pid} is not running. Cleaning up stale PID file.")
        PID_FILE.unlink()
        return

    print(f"Sending SIGTERM to trading daemon (PID={pid})...")
    os.kill(pid, signal.SIGTERM)

    # Wait for graceful shutdown (up to 30s)
    for i in range(30):
        time.sleep(1)
        if not _is_process_running(pid):
            print("Trading daemon stopped.")
            if PID_FILE.exists():
                PID_FILE.unlink()
            return

    print(f"Daemon still running after 30s. You may need to kill -9 {pid}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_process_running(pid: int) -> bool:
    """Check if a process with the given PID is running."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# competition
# ---------------------------------------------------------------------------

def cmd_competition(args):
    """Entry point for all competition sub-commands."""
    sub = args.comp_cmd

    if sub == "list":
        from competition.registry import CompetitionRegistry
        CompetitionRegistry().print_all()

    elif sub == "backtest":
        from competition.backtester import CompetitionBacktester
        bt = CompetitionBacktester()
        if args.strategy:
            bt.run(args.strategy, start=args.start, end=args.end, use_cache=not args.no_cache)
        else:
            bt.run_all(start=args.start, end=args.end, use_cache=not args.no_cache)

    elif sub == "compare":
        from competition.compare import print_leaderboard
        print_leaderboard()

    elif sub == "demo-start":
        from competition.registry import CompetitionRegistry
        reg = CompetitionRegistry()
        strategy_def = reg.get(args.strategy)

        if strategy_def.get("base_profile") == "custom":
            # Custom execution-loop strategy (e.g. elite_flow) — bypass TradingEngine
            _run_custom_strategy(args.strategy, strategy_def, foreground=getattr(args, "foreground", False))
        else:
            cfg = reg.to_portfolio_config(args.strategy)
            # Launch via the existing start command path
            args.config     = json.dumps([cfg])
            args.sandbox    = True
            args.foreground = getattr(args, "foreground", False)
            args.paper      = False
            print(f"Starting demo run for strategy: {args.strategy}")
            print(f"Portfolio config: {args.config}")
            cmd_start(args)

    elif sub == "demo-status":
        from competition.compare import compare_demo
        compare_demo()

    else:
        print(f"Unknown competition sub-command: {sub!r}")
        print("Available: list, backtest, compare, demo-start, demo-status")


def _run_custom_strategy(strategy_id: str, strategy_def: dict, foreground: bool = False) -> None:
    """
    Dispatch a 'base_profile: custom' strategy to its own execution module.

    Convention: strategy ID maps to competition/strategies/<id>.py which
    must expose a run(config, foreground) function.
    """
    import importlib
    import sys

    module_path = f"competition.strategies.{strategy_id}"
    try:
        mod = importlib.import_module(module_path)
    except ModuleNotFoundError:
        print(f"Error: no implementation found for custom strategy '{strategy_id}'")
        print(f"Expected module: {module_path}")
        sys.exit(1)

    config = strategy_def.get(f"{strategy_id}_config", {})
    effective_config = dict(config)
    profile_override = os.getenv("STRATEGY_PROFILE")
    if profile_override in ("demo", "live"):
        effective_config["profile"] = profile_override
    budget_override = os.getenv("YOLO_TOTAL_BUDGET")
    if strategy_id.startswith("yolo_") and budget_override:
        try:
            effective_config["total_budget"] = float(budget_override)
        except ValueError:
            pass
    print(f"Starting custom strategy: {strategy_id} (foreground={foreground})")
    if effective_config:
        print(f"Config: {json.dumps(effective_config, indent=2)}")

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(LOGS_DIR / f"{strategy_id}.log"),
        ],
    )

    mod.run(config=config, foreground=foreground)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Quantitative Trading Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # list-strategies
    sub.add_parser("list-strategies", help="Show available strategies")

    # competition
    p_comp = sub.add_parser("competition", help="Competition strategy tools")
    comp_sub = p_comp.add_subparsers(dest="comp_cmd", required=True)

    comp_sub.add_parser("list", help="List all registered competition strategies")

    p_bt = comp_sub.add_parser("backtest", help="Run backtest for one or all strategies")
    p_bt.add_argument("--strategy", "-s", default=None,
                      help="Strategy ID to backtest (omit to run all)")
    p_bt.add_argument("--start", default="2025-01-01", help="Backtest start date (default: 2025-01-01)")
    p_bt.add_argument("--end",   default="2026-03-31", help="Backtest end date (default: 2026-03-31)")
    p_bt.add_argument("--no-cache", action="store_true", default=False,
                      help="Force re-fetch price data (skip local cache)")

    comp_sub.add_parser("compare", help="Compare all strategies (backtest + live demo)")

    p_demo = comp_sub.add_parser("demo-start", help="Start a strategy in demo mode")
    p_demo.add_argument("--strategy", "-s", required=True, help="Strategy ID to demo run")
    p_demo.add_argument("--foreground", "-f", action="store_true", default=False,
                        help="Run in foreground (don't daemonize)")

    comp_sub.add_parser("demo-status", help="Show current performance of all demo runs")

    # start
    p_start = sub.add_parser("start", help="Start the trading daemon")
    p_start.add_argument(
        "--config", required=True,
        help='JSON array: [{"id":"name","strategy":"id","profile":"daily","capital":5000}]',
    )
    p_start.add_argument(
        "--sandbox", action="store_true", default=True,
        help="Use OKX demo account mode (default: True)",
    )
    p_start.add_argument(
        "--live", action="store_true", default=False,
        help="Use real OKX exchange (disables sandbox)",
    )
    p_start.add_argument(
        "--foreground", "-f", action="store_true", default=False,
        help="Run in foreground (don't daemonize)",
    )
    p_start.add_argument(
        "--paper", action="store_true", default=False,
        help="Paper trading: real mainnet prices, SimulatedExecution (no real orders sent)",
    )

    # session
    p_session = sub.add_parser("session", help="Multi-session management")
    sess_sub = p_session.add_subparsers(dest="sess_cmd", required=True)

    p_create = sess_sub.add_parser("create", help="Create a new trading session")
    p_create.add_argument("--strategy", "-s", required=True, help="Strategy ID (e.g. elite_flow)")
    p_create.add_argument("--id", default=None, help="Custom session ID (auto-generated if omitted)")
    p_create.add_argument("--config", default=None, help='JSON config overrides (e.g. \'{"symbols":["BTC-USDT-SWAP"]}\')')

    sess_sub.add_parser("list", help="List all sessions with status")

    p_stop = sess_sub.add_parser("stop", help="Stop a session")
    p_stop.add_argument("session_id", help="Session ID to stop")

    sess_sub.add_parser("stop-all", help="Stop all running sessions")

    p_daemon = sess_sub.add_parser("daemon", help="Run session daemon (manages all active sessions)")
    p_daemon.add_argument("--foreground", "-f", action="store_true", default=False,
                          help="Run in foreground (don't daemonize)")

    # status
    sub.add_parser("status", help="Show current trading status from logs")

    # stop
    sub.add_parser("stop", help="Stop the trading daemon")

    args = parser.parse_args()
    if hasattr(args, "live") and args.live:
        args.sandbox = False
    return args


if __name__ == "__main__":
    args = parse_args()
    def cmd_session_dispatch(args):
        from session import cmd_session
        cmd_session(args)

    commands = {
        "list-strategies": cmd_list_strategies,
        "competition":     cmd_competition,
        "session":         cmd_session_dispatch,
        "start":           cmd_start,
        "status":          cmd_status,
        "stop":            cmd_stop,
    }
    commands[args.command](args)
