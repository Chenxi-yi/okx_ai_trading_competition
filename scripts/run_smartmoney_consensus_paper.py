#!/usr/bin/env python3
"""Paper runner for smart-money weighted consensus candidate.

This is simulation-only. It reads the latest smart-money diffusion panel,
updates a local paper book, and writes state/ledger/equity artifacts. It never
places exchange orders.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from backtest_smartmoney_consensus import build_signals


ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / "engine" / "data" / "smartmoney_diffusion"
RESEARCH_ROOT = ROOT / "engine" / "research" / "reports" / "smartmoney_diffusion"
LOG_DIR = ROOT / "engine" / "logs" / "smartmoney_consensus_paper"
CONTROL_DIR = ROOT / "engine" / "control"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run smartmoney consensus paper shadow")
    p.add_argument("--state-id", default="smartmoney_weighted_consensus")
    p.add_argument("--environment", default="competition")
    p.add_argument("--panel", default="auto")
    p.add_argument("--initial-capital", type=float, default=3000.0)
    p.add_argument("--fixed-notional", type=float, default=117.0)
    p.add_argument("--hold-hours", type=int, default=12)
    p.add_argument("--max-positions", type=int, default=4)
    p.add_argument("--min-traders", type=int, default=3)
    p.add_argument("--min-notional", type=float, default=50_000.0)
    p.add_argument("--weighted-threshold", type=float, default=0.80)
    p.add_argument("--fee-bps-per-side", type=float, default=5.0)
    p.add_argument("--slippage-bps-per-side", type=float, default=2.0)
    p.add_argument("--allow-long", action="store_true", default=True)
    p.add_argument("--allow-short", action="store_true", default=True)
    p.add_argument("--loop", action="store_true")
    p.add_argument("--interval-sec", type=float, default=900.0)
    p.add_argument("--max-cycles", type=int, default=0)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    stop_path = CONTROL_DIR / f"smartmoney_consensus_paper_{args.state_id}_{args.environment}.stop"
    try:
        stop_path.unlink()
    except FileNotFoundError:
        pass
    cycles = 0
    while True:
        if stop_path.exists():
            _write_scheduler(args, "stopped", cycles)
            break
        try:
            state = _run_cycle(args)
            _write_state(args, state)
            cycles += 1
            _write_scheduler(args, "running", cycles, {"last_error": None})
        except Exception as exc:
            cycles += 1
            _write_scheduler(args, "error", cycles, {"last_error": str(exc)})
            if not args.loop:
                raise
        if not args.loop or (args.max_cycles > 0 and cycles >= args.max_cycles):
            _write_scheduler(args, "completed", cycles)
            break
        time.sleep(max(5.0, float(args.interval_sec)))
    return 0


def _run_cycle(args: argparse.Namespace) -> dict[str, Any]:
    panel_path = _resolve_panel(args.panel)
    panel = pd.read_csv(panel_path)
    panel["ts"] = pd.to_datetime(panel["ts"], utc=True, errors="coerce")
    panel = panel.dropna(subset=["ts", "close"]).sort_values(["ts", "ccy"]).reset_index(drop=True)
    if panel.empty:
        raise RuntimeError(f"empty panel: {panel_path}")
    now_ts = pd.Timestamp(panel["ts"].max())
    state = _load_state(args)
    positions = {str(k): dict(v) for k, v in dict(state.get("positions") or {}).items()}
    realized_nav = float(state.get("realized_nav") or args.initial_capital)
    ledger: list[dict[str, Any]] = []
    close_lookup = {(str(row.ccy), pd.Timestamp(row.ts)): float(row.close) for row in panel.itertuples() if _valid(row.close)}

    for key, pos in list(positions.items()):
        exit_ts = pd.Timestamp(pos.get("exit_ts"))
        if exit_ts.tzinfo is None:
            exit_ts = exit_ts.tz_localize("UTC")
        if now_ts < exit_ts:
            continue
        exit_price = close_lookup.get((str(pos.get("ccy") or key), exit_ts))
        if not _valid(exit_price):
            latest = _latest_close(panel, str(pos.get("ccy") or key), now_ts)
            exit_price = latest if _valid(latest) else None
        if not _valid(exit_price):
            continue
        pnl, net_return = _pnl(pos, float(exit_price), args)
        realized_nav += pnl
        ledger.append({**_event(now_ts, "exit", key, pos.get("side"), "horizon"), "pnl": pnl, "net_return": net_return, "exit_price": exit_price})
        positions.pop(key, None)

    slots = max(0, int(args.max_positions) - len(positions))
    if slots > 0:
        latest_group = panel[panel["ts"] == now_ts].copy()
        open_ccys = {str(pos.get("ccy") or key) for key, pos in positions.items()}
        signals = [sig for sig in build_signals(latest_group, args) if sig["ccy"] not in open_ccys]
        for sig in signals[:slots]:
            ccy = str(sig["ccy"])
            entry = float(sig["close"])
            if not _valid(entry) or entry <= 0:
                continue
            exit_ts = now_ts + pd.Timedelta(hours=int(args.hold_hours))
            positions[ccy] = {
                "ccy": ccy,
                "symbol": f"{ccy}/USDT",
                "side": str(sig["side"]),
                "entry_ts": now_ts.isoformat(),
                "exit_ts": exit_ts.isoformat(),
                "entry_price": entry,
                "notional_usdt": float(args.fixed_notional),
                "signal_score": float(sig["score"]),
                "source": "smartmoney_weighted_consensus_v1",
            }
            ledger.append({**_event(now_ts, "entry", ccy, sig["side"], "smartmoney_weighted_consensus_v1"), "entry_price": entry, "notional_usdt": float(args.fixed_notional), "signal_score": float(sig["score"])})

    positions = _mark_positions(positions, panel, now_ts, args)
    nav = realized_nav + sum(float(pos.get("unrealized_pnl") or 0.0) for pos in positions.values())
    equity = _upsert(list(state.get("equity") or []), {"ts": now_ts.isoformat(), "nav": nav, "open_positions": len(positions)})[-240:]
    ledger_tail = (list(state.get("ledger_tail") or []) + ledger)[-120:]
    return {
        "available": True,
        "state_id": args.state_id,
        "strategy_id": "smartmoney_weighted_consensus_v1",
        "environment": args.environment,
        "mode": "paper",
        "source_mode": "smartmoney_panel",
        "source_panel": str(panel_path.relative_to(ROOT)),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "timestamp": now_ts.isoformat(),
        "cash": realized_nav,
        "nav": nav,
        "realized_nav": realized_nav,
        "realized_pnl": realized_nav - float(args.initial_capital),
        "unrealized_pnl": nav - realized_nav,
        "positions": positions,
        "metrics": _metrics(equity, float(args.initial_capital)),
        "equity": equity,
        "ledger_tail": ledger_tail,
        "cycle_events": ledger,
    }


def _resolve_panel(value: str) -> Path:
    if value != "auto":
        return Path(value)
    candidates = list(DATA_ROOT.glob("run_*/smartmoney_diffusion_panel.csv")) + list(RESEARCH_ROOT.glob("run_*/smartmoney_diffusion_panel.csv"))
    if not candidates:
        raise RuntimeError("no smartmoney_diffusion_panel.csv found")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _load_state(args: argparse.Namespace) -> dict[str, Any]:
    path = LOG_DIR / f"{args.state_id}_{args.environment}.json"
    try:
        return json.loads(path.read_text())
    except Exception:
        return {"positions": {}, "ledger_tail": [], "equity": [], "realized_nav": float(args.initial_capital)}


def _write_state(args: argparse.Namespace, state: dict[str, Any]) -> None:
    prefix = f"{args.state_id}_{args.environment}"
    (LOG_DIR / f"{prefix}.json").write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    if state.get("equity"):
        _append_jsonl(LOG_DIR / f"{prefix}_equity.jsonl", [state["equity"][-1]])
    if state.get("cycle_events"):
        _append_jsonl(LOG_DIR / f"{prefix}_ledger.jsonl", list(state["cycle_events"]))


def _write_scheduler(args: argparse.Namespace, status: str, cycles: int, extra: dict[str, Any] | None = None) -> None:
    payload = {"scheduler_status": status, "cycles": cycles, "heartbeat_at": datetime.now(timezone.utc).isoformat(), "interval_sec": args.interval_sec}
    if extra:
        payload.update(extra)
    (LOG_DIR / f"{args.state_id}_{args.environment}_scheduler.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _append_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("a") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")


def _mark_positions(positions: dict[str, dict[str, Any]], panel: pd.DataFrame, now_ts: pd.Timestamp, args: argparse.Namespace) -> dict[str, dict[str, Any]]:
    out = {}
    for key, pos in positions.items():
        p = dict(pos)
        mark = _latest_close(panel, str(p.get("ccy") or key), now_ts)
        if _valid(mark):
            pnl, net = _pnl(p, float(mark), args)
            p["mark_price"] = float(mark)
            p["unrealized_pnl"] = pnl
            p["net_return"] = net
        out[key] = p
    return out


def _latest_close(panel: pd.DataFrame, ccy: str, ts: pd.Timestamp) -> float | None:
    rows = panel[(panel["ccy"].astype(str) == ccy) & (panel["ts"] <= ts)].sort_values("ts")
    if rows.empty:
        return None
    return float(rows.iloc[-1]["close"])


def _pnl(pos: dict[str, Any], price: float, args: argparse.Namespace) -> tuple[float, float]:
    entry = float(pos.get("entry_price") or 0.0)
    notional = float(pos.get("notional_usdt") or 0.0)
    side = str(pos.get("side") or "")
    if entry <= 0 or notional <= 0 or not _valid(price):
        return 0.0, 0.0
    raw = price / entry - 1.0
    gross = raw if side == "long" else -raw
    cost = 2.0 * (float(args.fee_bps_per_side) + float(args.slippage_bps_per_side)) / 10000.0
    net = gross - cost
    return notional * net, net


def _metrics(equity: list[dict[str, Any]], initial: float) -> dict[str, Any]:
    navs = [float(row.get("nav")) for row in equity if _valid(row.get("nav"))]
    if not navs:
        return {"initial_nav": initial, "total_return": 0.0, "max_drawdown": 0.0}
    peak = navs[0]
    max_dd = 0.0
    for nav in navs:
        peak = max(peak, nav)
        if peak > 0:
            max_dd = min(max_dd, nav / peak - 1.0)
    return {"initial_nav": initial, "total_return": navs[-1] / initial - 1.0, "max_drawdown": max_dd}


def _upsert(rows: list[dict[str, Any]], point: dict[str, Any]) -> list[dict[str, Any]]:
    kept = [row for row in rows if str(row.get("ts")) != str(point.get("ts"))]
    kept.append(point)
    return sorted(kept, key=lambda row: str(row.get("ts")))


def _event(ts: pd.Timestamp, event: str, symbol: Any, side: Any, reason: str) -> dict[str, Any]:
    return {"ts": ts.isoformat(), "event": event, "symbol": symbol, "side": side, "reason": reason, "pnl": None, "net_return": None}


def _valid(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except Exception:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
