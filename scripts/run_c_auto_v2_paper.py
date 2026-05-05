#!/usr/bin/env python3
"""Paper-dry runner for the C-Auto v2 fixed-notional portfolio stream."""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
ENGINE_DIR = ROOT / "engine"
PAPER_DIR = ENGINE_DIR / "logs" / "c_auto_v2_paper"
CONTROL_DIR = ENGINE_DIR / "control"
DEFAULT_SOURCE = "c_auto_v2_portfolio_backtest_fixed1000_conservative_v1"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run C-Auto v2 fixed1000 paper-dry stream")
    p.add_argument("--state-id", default="fixed1000_conservative")
    p.add_argument("--environment", default="personal", choices=["personal", "demo", "competition"])
    p.add_argument("--source-backtest", default=DEFAULT_SOURCE)
    p.add_argument("--initial-capital", type=float, default=1000.0)
    p.add_argument("--loop", action="store_true")
    p.add_argument("--interval-sec", type=float, default=60.0)
    p.add_argument("--max-cycles", type=int, default=0)
    p.add_argument("--start-from-latest", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    PAPER_DIR.mkdir(parents=True, exist_ok=True)
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    stop_path = CONTROL_DIR / f"c_auto_v2_paper_{args.state_id}_{args.environment}.stop"
    try:
        stop_path.unlink()
    except FileNotFoundError:
        pass

    source_dir = ENGINE_DIR / "data" / "research" / "c_auto" / args.source_backtest
    equity = _read_frame(source_dir / "equity_curve.parquet", source_dir / "equity_curve.csv")
    trades = _read_frame(source_dir / "trades.parquet", source_dir / "trades.csv")
    if equity.empty:
        raise SystemExit(f"empty equity source: {source_dir}")
    equity["ts"] = pd.to_datetime(equity["ts"], utc=True)
    equity = equity.sort_values("ts").reset_index(drop=True)
    if not trades.empty:
        trades["entry_ts"] = pd.to_datetime(trades["entry_ts"], utc=True)
        trades["exit_ts"] = pd.to_datetime(trades["exit_ts"], utc=True)

    start_idx = max(0, len(equity) - 240) if args.start_from_latest else 0
    cycles = 0
    idx = start_idx
    while True:
        if stop_path.exists():
            _write_scheduler(args, "stopped", cycles)
            break
        if idx >= len(equity):
            idx = start_idx
        _write_state(args, equity, trades, idx)
        cycles += 1
        _write_scheduler(args, "running", cycles)
        if not args.loop:
            break
        if args.max_cycles > 0 and cycles >= args.max_cycles:
            _write_scheduler(args, "completed", cycles)
            break
        idx += 1
        time.sleep(max(1.0, float(args.interval_sec)))
    return 0


def _read_frame(parquet: Path, csv_path: Path) -> pd.DataFrame:
    if parquet.exists():
        return pd.read_parquet(parquet)
    if csv_path.exists():
        return pd.read_csv(csv_path)
    return pd.DataFrame()


def _write_state(args: argparse.Namespace, equity: pd.DataFrame, trades: pd.DataFrame, idx: int) -> None:
    row = equity.iloc[idx].to_dict()
    ts = pd.Timestamp(row["ts"])
    nav = float(row.get("nav_mtm", row.get("nav", args.initial_capital)) or args.initial_capital)
    realized_nav = float(row.get("realized_nav", nav) or nav)
    unrealized = float(row.get("unrealized_pnl", nav - realized_nav) or 0.0)
    open_trades = _open_trades(trades, ts)
    closed = _closed_trades(trades, ts)
    realized_pnl = realized_nav - float(args.initial_capital)
    positions = _positions(open_trades)
    ledger_tail = _ledger_tail(open_trades, closed, ts)
    equity_tail = _equity_tail(equity.iloc[: idx + 1])
    state = {
        "available": True,
        "state_id": args.state_id,
        "strategy_id": "c_auto_v2_fixed1000_conservative",
        "environment": args.environment,
        "mode": "paper",
        "source_backtest": args.source_backtest,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "timestamp": ts.isoformat(),
        "cash": realized_nav,
        "nav": nav,
        "realized_nav": realized_nav,
        "unrealized_pnl": unrealized,
        "realized_pnl": realized_pnl,
        "open_risk": sum(float(pos.get("risk_budget", 0.0)) for pos in positions.values()),
        "positions": positions,
        "live_gates_enabled": False,
        "live_gate_pass_count": 0,
        "metrics": _metrics(equity.iloc[: idx + 1], args.initial_capital),
        "equity": equity_tail,
        "ledger_tail": ledger_tail,
    }
    prefix = f"{args.state_id}_{args.environment}"
    (PAPER_DIR / f"{prefix}.json").write_text(json.dumps(state, indent=2, sort_keys=True))
    _write_jsonl(PAPER_DIR / f"{prefix}_equity.jsonl", equity_tail[-1:])
    if ledger_tail:
        _write_jsonl(PAPER_DIR / f"{prefix}_ledger.jsonl", ledger_tail[-5:])


def _open_trades(trades: pd.DataFrame, ts: pd.Timestamp) -> pd.DataFrame:
    if trades.empty:
        return trades
    return trades[(trades["entry_ts"] <= ts) & (trades["exit_ts"] > ts)].copy()


def _closed_trades(trades: pd.DataFrame, ts: pd.Timestamp) -> pd.DataFrame:
    if trades.empty:
        return trades
    closed = trades[trades["exit_ts"] <= ts].copy()
    return closed.tail(20)


def _positions(open_trades: pd.DataFrame) -> dict[str, dict[str, Any]]:
    positions: dict[str, dict[str, Any]] = {}
    for _, row in open_trades.iterrows():
        symbol = str(row["symbol"])
        positions[symbol] = {
            "side": row.get("side", "long"),
            "score": float(row.get("score", 0.0) or 0.0),
            "risk_budget": float(row.get("notional", 0.0) or 0.0),
            "entry_price": float(row.get("entry_price", 0.0) or 0.0),
            "stop_price": None,
            "tp1_price": None,
            "tp2_price": None,
            "regime": row.get("regime"),
            "entry_ts": pd.Timestamp(row["entry_ts"]).isoformat(),
            "exit_ts": pd.Timestamp(row["exit_ts"]).isoformat(),
            "horizon_hours": int(row.get("horizon_hours", 0) or 0),
        }
    return positions


def _ledger_tail(open_trades: pd.DataFrame, closed: pd.DataFrame, ts: pd.Timestamp) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for _, row in closed.tail(10).iterrows():
        events.append(
            {
                "ts": pd.Timestamp(row["exit_ts"]).isoformat(),
                "event": "exit",
                "symbol": row.get("symbol"),
                "side": row.get("side"),
                "reason": row.get("exit_reason", "horizon"),
                "pnl": float(row.get("pnl", 0.0) or 0.0),
                "net_return": float(row.get("net_return", 0.0) or 0.0),
            }
        )
    for _, row in open_trades.tail(5).iterrows():
        events.append(
            {
                "ts": ts.isoformat(),
                "event": "hold",
                "symbol": row.get("symbol"),
                "side": row.get("side"),
                "reason": row.get("regime", "open"),
                "pnl": None,
                "net_return": None,
            }
        )
    return sorted(events, key=lambda item: item["ts"])[-20:]


def _equity_tail(equity: pd.DataFrame) -> list[dict[str, Any]]:
    out = []
    for _, row in equity.tail(240).iterrows():
        nav = float(row.get("nav_mtm", row.get("nav", 0.0)) or 0.0)
        out.append(
            {
                "ts": pd.Timestamp(row["ts"]).isoformat(),
                "nav": nav,
                "open_positions": int(row.get("open_positions", 0) or 0),
            }
        )
    return out


def _metrics(equity: pd.DataFrame, initial_capital: float) -> dict[str, Any]:
    navs = pd.to_numeric(equity.get("nav_mtm", equity.get("nav")), errors="coerce").dropna()
    if navs.empty:
        return {"initial_nav": initial_capital, "current_nav": initial_capital}
    peak = navs.cummax()
    dd = navs / peak - 1.0
    return {
        "initial_nav": float(initial_capital),
        "current_nav": float(navs.iloc[-1]),
        "total_return": float(navs.iloc[-1] / initial_capital - 1.0),
        "max_drawdown": float(dd.min()),
        "equity_points": int(len(navs)),
    }


def _write_scheduler(args: argparse.Namespace, status: str, cycles: int) -> None:
    prefix = f"{args.state_id}_{args.environment}"
    payload = {
        "scheduler_status": status,
        "heartbeat_at": datetime.now(timezone.utc).isoformat(),
        "cycles": cycles,
        "interval_sec": args.interval_sec,
        "state_id": args.state_id,
        "environment": args.environment,
    }
    (PAPER_DIR / f"{prefix}_scheduler.json").write_text(json.dumps(payload, indent=2, sort_keys=True))


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("a") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
