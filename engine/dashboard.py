#!/usr/bin/env python3
"""
dashboard.py — OKX Quant Trading Dashboard
Serves a Plotly.js frontend with live + backtest data.

Usage:
    python3 dashboard.py            # http://localhost:8080
    python3 dashboard.py --port 9000 --open
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

BASE_DIR   = Path(__file__).resolve().parent
LOGS_DIR   = BASE_DIR / "logs"
STATIC_DIR = BASE_DIR / "dashboard"
DEFAULT_ELITE_LEVERAGE = 2

# Cache backtest result so repeated page loads don't re-run it
_backtest_cache: Dict[str, Any] = {}
_backtest_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Data readers
# ---------------------------------------------------------------------------

def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text()) if path.exists() else None
    except Exception:
        return None


def load_swap_specs(profile: str) -> Dict[str, Dict[str, Any]]:
    return read_json(LOGS_DIR / f"okx_swap_instrument_specs_{profile}.json") or {}


def get_yolo_orchestrator_state_path(profile: str | None = None) -> Path:
    effective = profile or get_okx_default_profile()
    suffix = "_live" if effective == "live" else ""
    return LOGS_DIR / f"yolo_orchestrator{suffix}.json"


def get_okx_default_profile() -> str:
    cfg_path = Path.home() / ".okx" / "config.toml"
    try:
        if cfg_path.exists():
            text = cfg_path.read_text()
            match = re.search(r'^\s*default_profile\s*=\s*"([^"]+)"\s*$', text, re.MULTILINE)
            if match:
                profile = match.group(1).strip()
                if profile:
                    return profile
    except Exception:
        pass
    return "demo"


def run_okx_json(args: List[str], profile: str | None = None, timeout: int = 15) -> Any:
    profile = profile or get_okx_default_profile()
    try:
        r = subprocess.run(
            ["okx", "--profile", profile, "--json"] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(BASE_DIR),
        )
        if r.returncode != 0 or not r.stdout.strip():
            return None
        data = json.loads(r.stdout)
        if isinstance(data, dict) and "data" in data:
            return data.get("data")
        return data
    except Exception:
        return None


def read_csv_as_dicts(path: Path) -> List[Dict]:
    if not path.exists():
        return []
    try:
        with open(path) as f:
            reader = csv.DictReader(f)
            return list(reader)
    except Exception:
        return []


def read_jsonl_tail(path: Path, n: int = 50) -> List[Dict]:
    if not path.exists():
        return []
    try:
        lines = path.read_text().splitlines()
        return [json.loads(l) for l in lines[-n:] if l.strip()]
    except Exception:
        return []


SIGNAL_RE = re.compile(
    r"sym=(?P<symbol>[A-Z0-9\-]+)\s+conviction=(?P<conviction>-?\d+(?:\.\d+)?)\s+"
    r"state=(?P<from_state>[A-Z]+)→(?P<to_state>[A-Z]+)\s+flow=(?P<flow>-?\d+(?:\.\d+)?)\s+"
    r"crowd=(?P<crowd>-?\d+(?:\.\d+)?)\s+regime=(?P<regime>-?\d+(?:\.\d+)?)"
)
RECONCILE_RE = re.compile(
    r"sym=(?P<symbol>[A-Z0-9\-]+)\s+side=(?P<side>[a-z]+)\s+state=(?P<state>[A-Z]+)\s+"
    r"entry=(?P<entry>-?\d+(?:\.\d+)?)\s+price=(?P<price>-?\d+(?:\.\d+)?)\s+"
    r"pnl=(?P<pnl_pct>-?\d+(?:\.\d+)?)%\s+held=(?P<held_min>\d+)m\s+"
    r"flow=(?P<flow>-?\d+(?:\.\d+)?)\s+crowd=(?P<crowd>-?\d+(?:\.\d+)?)\s+regime=(?P<regime>-?\d+(?:\.\d+)?)"
)
PLACED_RE = re.compile(
    r"EliteFlow \[(?P<symbol>[A-Z0-9\-]+)\]: placed side=(?P<side>buy|sell)\s+sz=(?P<contracts>\d+)"
)
CLOSED_RE = re.compile(r"EliteFlow \[(?P<symbol>[A-Z0-9\-]+)\]: closed")
LEVERAGE_RE = re.compile(
    r"EliteFlow \[(?P<symbol>[A-Z0-9\-]+)\]: leverage set to (?P<leverage>\d+)x"
)


def _parse_log_prefix(line: str) -> Dict[str, str]:
    match = re.match(
        r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[(?P<level>[A-Z]+)\] (?P<logger>[^:]+): (?P<message>.*)",
        line,
    )
    return match.groupdict() if match else {"ts": "", "level": "", "logger": "", "message": line}


def _elite_trigger_text(signal: Dict[str, Any]) -> str:
    conviction = float(signal.get("conviction", 0.0))
    flow = float(signal.get("flow", 0.0))
    crowd = float(signal.get("crowd", 0.0))
    regime = float(signal.get("regime", 0.0))
    direction = "LONG bias" if conviction >= 0 else "SHORT bias"
    components = [
        f"conviction {conviction:+.3f}",
        f"flow {flow:+.3f}",
        f"crowd {crowd:+.3f}",
        f"regime {regime:+.3f}",
    ]
    return f"{direction}; " + ", ".join(components)


def api_live(_qs: Dict) -> Dict:
    summary  = read_json(LOGS_DIR / "summary.json") or {}
    hb       = read_json(LOGS_DIR / "heartbeat.json") or {}
    pid_file = BASE_DIR / "control" / "trading.pid"
    pid_raw  = pid_file.read_text().strip() if pid_file.exists() else None
    if not pid_raw:
        pid_raw = str(summary.get("pid") or "").strip() or None
    alive = False
    if pid_raw:
        try:
            os.kill(int(pid_raw), 0)
            alive = True
        except Exception:
            pass
    return {"summary": summary, "heartbeat": hb, "daemon_alive": alive}


def api_performance(qs: Dict) -> List[Dict]:
    pid = qs.get("portfolio", [None])[0]
    rows = read_csv_as_dicts(LOGS_DIR / "performance.csv")
    if pid:
        rows = [r for r in rows if r.get("portfolio_id") == pid]
    return rows


def api_trades(qs: Dict) -> List[Dict]:
    pid   = qs.get("portfolio", [None])[0]
    limit = int(qs.get("limit", ["200"])[0])
    rows  = read_csv_as_dicts(LOGS_DIR / "trades.csv")
    if pid:
        rows = [r for r in rows if r.get("portfolio_id") == pid]
    return rows[-limit:]


def api_signals(qs: Dict) -> List[Dict]:
    pid   = qs.get("portfolio", ["daily_combined"])[0]
    limit = int(qs.get("limit", ["30"])[0])
    return read_jsonl_tail(LOGS_DIR / "signals" / f"{pid}.jsonl", limit)


def api_risk(_qs: Dict) -> Dict:
    """Risk matrix: per-portfolio CB, vol regime, correlation, exposure, drawdown."""
    summary = read_json(LOGS_DIR / "summary.json") or {}
    portfolios = summary.get("portfolios", {})
    result = {}
    for pid, snap in portfolios.items():
        risk = snap.get("risk", {})
        result[pid] = {
            "nav":          snap.get("nav", 0),
            "capital":      snap.get("capital", 0),
            "pnl_pct":      snap.get("pnl_pct", 0),
            "drawdown_pct": snap.get("drawdown_pct", 0),
            "peak_nav":     snap.get("peak_nav", 0),
            "gross_exp":    snap.get("gross_exp", 0),
            "net_exp":      snap.get("net_exp", 0),
            "long_exposure": snap.get("long_exposure", 0),
            "short_exposure": snap.get("short_exposure", 0),
            "n_positions":  snap.get("n_positions", 0),
            "cb_state":     risk.get("cb", "NORMAL"),
            "vol_regime":   risk.get("vol", "MEDIUM"),
            "risk_scalar":  risk.get("scalar", 1.0),
            "total_fees":   snap.get("total_fees", 0),
        }

    # Also collect recent risk events from portfolio JSONL
    risk_events = []
    for pid in portfolios:
        events = read_jsonl_tail(LOGS_DIR / f"{pid}.jsonl", 100)
        for e in events:
            if e.get("event") in ("risk_check", "rebalance"):
                r = e.get("risk", {})
                if r:
                    risk_events.append({
                        "ts":        e.get("ts", ""),
                        "portfolio": pid,
                        "cb":        r.get("circuit_breaker_state", r.get("cb", "")),
                        "vol":       r.get("vol_regime", r.get("vol", "")),
                        "scalar":    r.get("combined_scalar", r.get("scalar", 1.0)),
                        "dd_pct":    r.get("drawdown_pct", e.get("drawdown_pct", 0)),
                        "action":    e.get("action", r.get("action", "")),
                    })
    risk_events.sort(key=lambda x: x.get("ts", ""), reverse=True)

    return {"portfolios": result, "events": risk_events[:50]}


def api_account(_qs: Dict) -> Dict:
    """Live account data from OKX — balance + positions. Competition-focused."""
    result = {"balance": {}, "positions": [], "error": None, "balance_view": {}, "positions_view": []}

    # Balance
    try:
        r = subprocess.run(
            ["python3", str(BASE_DIR / ".." / ".claude" / "tools" / "check_balance.py"), "--json"],
            capture_output=True, text=True, timeout=15, cwd=str(BASE_DIR),
        )
        if r.returncode == 0 and r.stdout.strip():
            result["balance"] = json.loads(r.stdout)
    except Exception as e:
        result["error"] = f"balance: {e}"

    # Positions
    try:
        r = subprocess.run(
            ["python3", str(BASE_DIR / ".." / ".claude" / "tools" / "check_positions.py"), "--json"],
            capture_output=True, text=True, timeout=15, cwd=str(BASE_DIR),
        )
        if r.returncode == 0 and r.stdout.strip():
            result["positions"] = json.loads(r.stdout)
    except Exception as e:
        result["error"] = (result.get("error") or "") + f" positions: {e}"

    # Custom strategy status from logs — legacy top-level + session-based
    strategies = {}
    for name in ("elite_flow",):
        logfile = LOGS_DIR / f"{name}.log"
        if logfile.exists():
            lines = logfile.read_text().splitlines()
            strategies[name] = {
                "log_lines": len(lines),
                "last_line": lines[-1] if lines else "",
                "modified": datetime.fromtimestamp(logfile.stat().st_mtime, tz=timezone.utc).isoformat(),
            }
    # Session-based logs (all strategies under SessionDaemon)
    sessions_dir = LOGS_DIR / "sessions"
    if sessions_dir.exists():
        for session_dir in sorted(sessions_dir.iterdir()):
            if session_dir.is_dir():
                logfile = session_dir / "strategy.log"
                if logfile.exists():
                    try:
                        lines = logfile.read_text().splitlines()
                        strategies[session_dir.name] = {
                            "log_lines": len(lines),
                            "last_line": lines[-1] if lines else "",
                            "modified": datetime.fromtimestamp(
                                logfile.stat().st_mtime, tz=timezone.utc
                            ).isoformat(),
                        }
                    except Exception:
                        pass
    result["custom_strategies"] = strategies

    balance = result.get("balance")
    if isinstance(balance, list) and balance:
        bal0 = balance[0]
        details = bal0.get("details", [])
        usdt = next((d for d in details if d.get("ccy") == "USDT"), {})
        result["balance_view"] = {
            "total_eq": float(bal0.get("totalEq") or 0),
            "upl": float(bal0.get("upl") or 0),
            "usdt_eq": float(usdt.get("eq") or 0),
            "usdt_avail": float(usdt.get("availEq") or 0),
        }

    raw_positions = result.get("positions")
    if isinstance(raw_positions, list):
        for pos in raw_positions:
            contracts = abs(float(pos.get("pos") or 0))
            side = "long" if float(pos.get("pos") or 0) > 0 else "short"
            result["positions_view"].append({
                "instId": pos.get("instId", ""),
                "side": side,
                "contracts": contracts,
                "avgPx": float(pos.get("avgPx") or 0),
                "markPx": float(pos.get("markPx") or 0),
                "lever": pos.get("lever") or pos.get("leverage") or "",
                "marginMode": pos.get("mgnMode") or "",
                "upl": float(pos.get("upl") or 0),
                "notionalUsd": float(pos.get("notionalUsd") or pos.get("notionalUsdPx") or 0),
            })

    return result


def _infer_trade_rows(history: List[Dict]) -> List[Dict]:
    rows: List[Dict] = []
    for h in history or []:
        leverage = int(h.get("leverage") or 0)
        entry_px = float(h.get("entry_price") or 0)
        sz = float(h.get("sz") or 0)
        inst = h.get("inst_id", "")
        notional = sz * entry_px if entry_px > 0 else 0.0
        margin = (notional / leverage) if leverage > 0 else 0.0
        rows.append({
            "ts": h.get("time", ""),
            "inst_id": inst,
            "side": h.get("side", ""),
            "action": "close",
            "pnl": round(float(h.get("pnl") or 0), 2),
            "reason": h.get("reason", ""),
            "round": int(h.get("round") or 0),
            "capital_deployed": sz,
            "leverage": leverage,
            "entry_price": entry_px,
            "margin": round(margin, 2),
            "fee_est": 0.0,
        })
    return rows


def _fetch_live_positions() -> List[Dict]:
    positions = run_okx_json(["account", "positions", "--instType", "SWAP"], timeout=20)
    if not isinstance(positions, list):
        return []
    return [p for p in positions if abs(float(p.get("pos") or 0)) > 0]


def _fetch_live_bills(limit: int = 50) -> List[Dict]:
    bills = run_okx_json(["account", "bills", "--limit", str(limit)], timeout=20)
    if not isinstance(bills, list):
        return []
    return bills


def _normalize_yolo_live_bill(bill: Dict) -> Dict:
    ts_ms = int(bill.get("ts") or bill.get("fillTime") or 0)
    ts_iso = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat() if ts_ms else ""
    fee = float(bill.get("fee") or 0)
    pnl = float(bill.get("pnl") or 0)
    return {
        "ts": ts_iso,
        "inst_id": bill.get("instId", ""),
        "side": "long" if str(bill.get("subType", "")) == "1" else "short",
        "action": "exchange_bill",
        "pnl": pnl,
        "reason": "EXCHANGE_BILL",
        "round": 0,
        "capital_deployed": float(bill.get("sz") or 0),
        "leverage": None,
        "entry_price": float(bill.get("px") or 0),
        "margin": 0.0,
        "fee_est": abs(fee),
        "ord_id": bill.get("ordId", ""),
        "tag": bill.get("tag", ""),
        "mgn_mode": bill.get("mgnMode", ""),
    }


def _build_momentum_dashboard_data(state_path: Path, profile: str) -> Dict:
    state = read_json(state_path) or {}
    swap_specs = load_swap_specs(profile)
    strategy_status = str(state.get("status") or "").upper()
    slot_status = "running"
    if strategy_status in ("TARGET_HIT", "SUCCEEDED"):
        slot_status = "succeeded"
    elif strategy_status in ("DRAINED", "LIQUIDATED", "ROUND_LOST"):
        slot_status = "drained"
    elif strategy_status == "RECHARGE_REQUIRED":
        slot_status = "recharge_required"

    realized_pnl = round(float(state.get("realized_pnl") or 0) - float(state.get("total_fees") or 0), 2)
    current_position = None
    if state.get("inst_id") and float(state.get("sz") or 0) > 0:
        lev = int(state.get("leverage") or 0)
        spec = swap_specs.get(state.get("inst_id"), {})
        current_position = {
            "inst_id": state.get("inst_id"),
            "side": state.get("side"),
            "entry_price": float(state.get("entry_price") or 0),
            "mark_price": 0.0,
            "leverage": lev,
            "planned_leverage": lev,
            "max_allowed_leverage": round(float(spec.get("max_leverage") or 0), 2) if spec else None,
            "contracts": float(state.get("sz") or 0),
            "unrealized_pnl": 0.0,
        }

    scan = state.get("scan_status") or {}
    top_candidates = []
    for candidate in scan.get("top_candidates") or []:
        item = dict(candidate)
        spec = swap_specs.get(item.get("inst_id", ""), {})
        desired_leverage = 50
        atr_pct = float(item.get("atr_pct") or 0)
        if atr_pct > 3.0:
            desired_leverage = 30
        elif 0 < atr_pct < 1.0:
            desired_leverage = 75
        max_allowed = float(spec.get("max_leverage") or 0)
        effective = min(desired_leverage, int(max_allowed)) if max_allowed > 0 else desired_leverage
        item["desired_leverage"] = desired_leverage
        item["max_allowed_leverage"] = round(max_allowed, 2) if max_allowed > 0 else None
        item["effective_leverage"] = effective
        top_candidates.append(item)
    if top_candidates:
        scan = {**scan, "top_candidates": top_candidates}
    started_at = (
        state.get("entry_time")
        or scan.get("started_at")
        or None
    )
    trades = _infer_trade_rows(state.get("history") or [])
    data = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "profile": state.get("profile") or profile,
        "strategy": "yolo_momentum",
        "total_budget": round(float(state.get("total_budget") or state.get("cumulative_invested") or state.get("current_margin") or 0), 2),
        "total_deployed": 1,
        "total_succeeded": 1 if slot_status == "succeeded" else 0,
        "total_drained": 1 if slot_status == "drained" else 0,
        "total_running": 1 if slot_status == "running" else 0,
        "total_pending": 0,
        "overall_roi_pct": 0.0,
        "overall_pnl": realized_pnl,
        "overall_invested": round(float(state.get("cumulative_invested") or 0), 2),
        "slots": [{
            "id": 1,
            "status": slot_status,
            "started_at": started_at,
            "finished_at": None,
            "total_budget": round(float(state.get("total_budget") or 0), 2),
            "round": int(state.get("round") or 0),
            "cumulative_invested": round(float(state.get("cumulative_invested") or 0), 2),
            "realized_pnl": realized_pnl,
            "unrealized_pnl": 0.0,
            "total_pnl": realized_pnl,
            "roi_pct": 0.0,
            "nav_history": [],
            "trades": trades,
            "current_position": current_position,
            "scan_status": {
                "scan_status": scan,
                "last_block_reason": state.get("last_block_reason", ""),
                "strategy_status": strategy_status,
            },
            "state_source": state_path.name,
            "fees_paid": round(float(state.get("total_fees") or 0), 4),
            "strategy_status": strategy_status,
        }],
    }
    return data


def _apply_slot_state_overlay(slot: Dict, profile: str | None = None) -> None:
    effective = profile or get_okx_default_profile()
    suffix = "_live" if effective == "live" else ""
    state_path = LOGS_DIR / f"yolo_slot_{slot.get('id')}{suffix}_state.json"
    state = read_json(state_path)
    legacy_path = LOGS_DIR / ("yolo_momentum_live_state.json" if effective == "live" else "yolo_momentum_state.json")
    legacy_state = None
    if slot.get("status") == "running" and legacy_path.exists():
        legacy_state = read_json(legacy_path)

    def _state_is_richer(candidate: Dict) -> bool:
        return bool(
            candidate and (
                candidate.get("inst_id")
                or candidate.get("history")
                or candidate.get("scan_status")
                or candidate.get("status") not in (None, "", "HUNTING")
            )
        )

    if legacy_state and (
        not state
        or not _state_is_richer(state)
        or legacy_path.stat().st_mtime > state_path.stat().st_mtime
    ):
        state = legacy_state
        state_path = legacy_path
    if not state:
        return

    slot["state_source"] = state_path.name
    slot["round"] = state.get("round", slot.get("round", 0))
    slot["cumulative_invested"] = round(float(state.get("cumulative_invested") or slot.get("cumulative_invested") or 0), 2)
    slot["fees_paid"] = round(float(state.get("total_fees") or 0), 4)
    slot["realized_pnl"] = round(float(state.get("realized_pnl") or 0) - float(state.get("total_fees") or 0), 2)
    slot["strategy_status"] = state.get("status", "")
    slot["scan_status"] = {
        "scan_status": state.get("scan_status", {}),
        "last_block_reason": state.get("last_block_reason", ""),
        "strategy_status": state.get("status", ""),
    }
    slot["strategy_status"] = state.get("status", "")
    if state.get("history"):
        slot["trades"] = _infer_trade_rows(state.get("history", []))
    if state.get("inst_id") and state.get("entry_price") and float(state.get("sz") or 0) > 0:
        slot["current_position"] = {
            "inst_id": state.get("inst_id"),
            "side": state.get("side"),
            "entry_price": float(state.get("entry_price") or 0),
            "leverage": int(state.get("leverage") or 0),
            "planned_leverage": int(state.get("leverage") or 0),
            "contracts": float(state.get("sz") or 0),
            "unrealized_pnl": round(float(slot.get("unrealized_pnl") or 0), 2),
        }


def _reconcile_yolo_with_exchange(data: Dict) -> Dict:
    positions = _fetch_live_positions()
    bills = _fetch_live_bills(limit=80)
    live_pos_map = {p.get("instId"): p for p in positions if p.get("instId")}
    sync_time = datetime.now(timezone.utc).isoformat()
    warnings: List[str] = []
    is_single_momentum = data.get("strategy") == "yolo_momentum"

    for slot in data.get("slots", []):
        slot_status = slot.get("status")

        # Finished slots should reflect the orchestrator snapshot that closed
        # them, not any stale per-slot state file left behind in HUNTING mode.
        if slot_status == "running" and not is_single_momentum:
            _apply_slot_state_overlay(slot, data.get("profile"))

        pos = slot.get("current_position") or {}
        inst_id = pos.get("inst_id")
        live_pos = live_pos_map.get(inst_id) if inst_id else None
        strategy_status = slot.get("strategy_status", "")

        # A finished slot must not retain stale open-position state or
        # unrealized PnL from historical snapshots.
        if slot_status in ("succeeded", "drained"):
            slot["current_position"] = None
            slot["unrealized_pnl"] = 0.0
            slot["total_pnl"] = round(float(slot.get("realized_pnl") or 0), 2)
            invested = float(slot.get("cumulative_invested") or 0)
            slot["roi_pct"] = round((slot["total_pnl"] / invested * 100) if invested > 0 else 0.0, 4)
            slot["live_sync_status"] = "finished_finalized"
            continue

        # A hunting slot with no inst_id must never absorb some other strategy's
        # only open exchange position. Match exchange state only by explicit inst_id.
        if slot_status == "running" and not inst_id:
            slot["current_position"] = None
            slot["unrealized_pnl"] = 0.0
            slot["total_pnl"] = round(float(slot.get("realized_pnl") or 0), 2)
            invested = float(slot.get("cumulative_invested") or 0)
            slot["roi_pct"] = round((slot["total_pnl"] / invested * 100) if invested > 0 else 0.0, 4)
            if strategy_status in ("HUNTING", "ROUND_LOST", "DONE", "TARGET_HIT", "RECHARGE_REQUIRED"):
                slot["live_sync_status"] = "idle_no_position"

        if live_pos:
            actual_side = "long" if float(live_pos.get("pos") or 0) > 0 else "short"
            actual_entry = float(live_pos.get("avgPx") or 0)
            actual_lever = float(live_pos.get("lever") or 0)
            actual_upl = float(live_pos.get("upl") or 0)
            actual_notional = float(live_pos.get("notionalUsd") or 0)
            pos.update({
                "inst_id": live_pos.get("instId", inst_id),
                "side": pos.get("side") or actual_side,
                "entry_price": actual_entry or float(pos.get("entry_price") or 0),
                "planned_leverage": pos.get("planned_leverage") or pos.get("leverage"),
                "max_allowed_leverage": pos.get("max_allowed_leverage"),
                "actual_leverage": actual_lever,
                "leverage": actual_lever or pos.get("leverage"),
                "mark_price": float(live_pos.get("markPx") or 0),
                "contracts": abs(float(live_pos.get("pos") or 0)),
                "notional_usd": actual_notional,
                "margin_mode": live_pos.get("mgnMode", ""),
                "exchange_realized_pnl": float(live_pos.get("realizedPnl") or 0),
                "unrealized_pnl": round(actual_upl, 2),
                "synced_from_exchange": True,
            })
            slot["current_position"] = pos
            slot["unrealized_pnl"] = round(actual_upl, 2)
            slot["total_pnl"] = round(float(slot.get("realized_pnl") or 0) + float(slot.get("unrealized_pnl") or 0), 2)
            invested = float(slot.get("cumulative_invested") or 0)
            slot["roi_pct"] = round((slot["total_pnl"] / invested * 100) if invested > 0 else 0.0, 4)
            slot["live_sync_status"] = "matched"
            slot["exchange_updated_at"] = sync_time
            slot["exchange_bills"] = [
                _normalize_yolo_live_bill(b) for b in bills
                if b.get("instId") == pos.get("inst_id")
            ][:10]
        elif slot_status == "running" and inst_id:
            slot["live_sync_status"] = "position_missing"
            warnings.append(f"slot {slot.get('id')} expects {inst_id} but no live OKX position matched")

    deployed = [s for s in data.get("slots", []) if s.get("status") != "pending"]
    finished = [s for s in deployed if s.get("status") in ("succeeded", "drained")]
    data["total_deployed"] = len(deployed)
    data["total_succeeded"] = sum(1 for s in deployed if s.get("status") == "succeeded")
    data["total_drained"] = sum(1 for s in deployed if s.get("status") == "drained")
    data["total_running"] = sum(1 for s in deployed if s.get("status") == "running")
    data["total_pending"] = sum(1 for s in data.get("slots", []) if s.get("status") == "pending")
    data["total_recharge_required"] = sum(1 for s in deployed if s.get("status") == "recharge_required")
    data["total_finished"] = len(finished)
    data["win_rate_pct"] = round((data["total_succeeded"] / len(finished) * 100) if finished else 0.0, 2)
    data["loss_rate_pct"] = round((data["total_drained"] / len(finished) * 100) if finished else 0.0, 2)
    realized_invested = round(sum(float(s.get("cumulative_invested") or 0) for s in finished), 2)
    realized_pnl = round(sum(float(s.get("realized_pnl") or 0) for s in finished), 2)
    running_slots = [s for s in deployed if s.get("status") == "running"]
    running_invested = round(sum(float(s.get("cumulative_invested") or 0) for s in running_slots), 2)
    running_unrealized_pnl = round(sum(float(s.get("unrealized_pnl") or 0) for s in running_slots), 2)
    running_total_pnl = round(sum(float(s.get("total_pnl") or 0) for s in running_slots), 2)

    data["realized_invested"] = realized_invested
    data["realized_pnl"] = realized_pnl
    data["realized_roi_pct"] = round((realized_pnl / realized_invested * 100) if realized_invested > 0 else 0.0, 4)
    data["running_invested"] = running_invested
    data["running_unrealized_pnl"] = running_unrealized_pnl
    data["running_total_pnl"] = running_total_pnl
    data["running_roi_pct"] = round((running_total_pnl / running_invested * 100) if running_invested > 0 else 0.0, 4)
    data["overall_invested"] = round(sum(float(s.get("cumulative_invested") or 0) for s in deployed), 2)
    data["overall_pnl"] = round(realized_pnl + running_total_pnl, 2)
    if data.get("strategy") == "yolo_momentum" and data.get("slots"):
        slot_budget = max(float(s.get("total_budget") or 0) for s in data.get("slots", []))
        if slot_budget > 0:
            data["total_budget"] = round(slot_budget, 2)
    invested = float(data.get("overall_invested") or 0)
    data["overall_roi_pct"] = round((data["overall_pnl"] / invested * 100) if invested > 0 else 0.0, 4)
    data["exchange_positions"] = [
        {
            "inst_id": p.get("instId", ""),
            "side": "long" if float(p.get("pos") or 0) > 0 else "short",
            "contracts": abs(float(p.get("pos") or 0)),
            "entry_price": float(p.get("avgPx") or 0),
            "mark_price": float(p.get("markPx") or 0),
            "actual_leverage": float(p.get("lever") or 0),
            "upl": float(p.get("upl") or 0),
        }
        for p in positions
    ]
    data["exchange_sync"] = {
        "updated_at": sync_time,
        "warnings": warnings,
        "open_positions": len(positions),
    }
    return data


def api_model_signals(qs: Dict) -> Dict:
    """Read custom strategy logs (elite_flow) + portfolio signal logs."""
    limit = int(qs.get("limit", ["100"])[0])
    result = {}

    # Custom strategy logs — scan session directories + legacy top-level logs
    log_sources: Dict[str, Path] = {}

    # Session-based logs (logs/sessions/<id>/strategy.log)
    sessions_dir = LOGS_DIR / "sessions"
    if sessions_dir.exists():
        for session_dir in sorted(sessions_dir.iterdir()):
            if session_dir.is_dir():
                logfile = session_dir / "strategy.log"
                if logfile.exists():
                    log_sources[session_dir.name] = logfile

    # Legacy top-level logs (logs/elite_flow.log)
    for name in ("elite_flow",):
        logfile = LOGS_DIR / f"{name}.log"
        if logfile.exists() and name not in log_sources:
            log_sources[name] = logfile

    for name, logfile in log_sources.items():
        try:
            lines = logfile.read_text().splitlines()
            entries = []
            for line in lines[-limit:]:
                if not line.strip():
                    continue
                entry = {"raw": line}
                parts = line.split("] ", 1)
                if len(parts) >= 2:
                    ts_level = parts[0]
                    entry["message"] = parts[1]
                    if "[" in ts_level:
                        ts_part, level_part = ts_level.rsplit("[", 1)
                        entry["ts"] = ts_part.strip()
                        entry["level"] = level_part.strip()
                raw = line.lower()
                if "signal=" in raw or "conviction=" in raw or "best=" in raw:
                    entry["type"] = "signal"
                elif "placed" in raw or "order" in raw:
                    entry["type"] = "order"
                elif "stop" in raw or "exit" in raw or "closed" in raw:
                    entry["type"] = "exit"
                elif "reconcile" in raw:
                    entry["type"] = "reconcile"
                elif "error" in raw or "fail" in raw:
                    entry["type"] = "error"
                else:
                    entry["type"] = "info"
                entries.append(entry)
            result[name] = entries
        except Exception:
            result[name] = []

    # Portfolio signal logs (bar-based strategies)
    sig_dir = LOGS_DIR / "signals"
    if sig_dir.exists():
        for f in sig_dir.glob("*.jsonl"):
            pid = f.stem
            entries = read_jsonl_tail(f, min(limit, 30))
            # Flatten signal data for display
            flat = []
            for e in entries:
                rec = {
                    "ts":     e.get("ts", ""),
                    "type":   "rebalance",
                    "n_raw":  e.get("n_raw_signals", 0),
                    "n_final": e.get("n_final_positions", 0),
                }
                # Top signals
                fw = e.get("final_weights", {})
                if fw:
                    top = sorted(fw.items(), key=lambda x: abs(x[1]), reverse=True)[:5]
                    rec["top_signals"] = {k: round(v, 4) for k, v in top}
                # Sleeve breakdown
                sleeves = e.get("sleeves", {})
                rec["sleeves"] = {
                    sl: {"n_long": d.get("n_long", 0), "n_short": d.get("n_short", 0)}
                    for sl, d in sleeves.items() if not d.get("error")
                }
                # Risk state
                risk = e.get("risk", {})
                if risk:
                    rec["risk"] = {
                        "cb": risk.get("circuit_breaker_state", ""),
                        "vol": risk.get("vol_regime", ""),
                        "scalar": risk.get("combined_scalar", 1.0),
                    }
                flat.append(rec)
            result[f"portfolio_{pid}"] = flat

    return result


def api_decision_journal(qs: Dict) -> Dict:
    limit = int(qs.get("limit", ["120"])[0])
    logfile = LOGS_DIR / "elite_flow.log"
    if not logfile.exists():
        return {"events": [], "latest_signal_by_symbol": {}, "latest_position_by_symbol": {}}

    lines = logfile.read_text().splitlines()[-4000:]
    leverage_by_symbol: Dict[str, int] = {}
    latest_signal_by_symbol: Dict[str, Dict[str, Any]] = {}
    latest_position_by_symbol: Dict[str, Dict[str, Any]] = {}
    events: List[Dict[str, Any]] = []
    last_raw = ""

    for line in lines:
        parsed = _parse_log_prefix(line)
        message = parsed["message"]
        if message == last_raw:
            continue
        last_raw = message

        if lev := LEVERAGE_RE.search(message):
            leverage_by_symbol[lev.group("symbol")] = int(lev.group("leverage"))
            events.append({
                "ts": parsed["ts"],
                "level": parsed["level"],
                "type": "config",
                "symbol": lev.group("symbol"),
                "title": f"Leverage set to {lev.group('leverage')}x",
                "detail": "Strategy startup configuration",
                "leverage": int(lev.group("leverage")),
            })
            continue

        if sig := SIGNAL_RE.search(message):
            signal = {
                "ts": parsed["ts"],
                "level": parsed["level"],
                "type": "signal",
                "symbol": sig.group("symbol"),
                "conviction": float(sig.group("conviction")),
                "flow": float(sig.group("flow")),
                "crowd": float(sig.group("crowd")),
                "regime": float(sig.group("regime")),
                "from_state": sig.group("from_state"),
                "to_state": sig.group("to_state"),
            }
            signal["title"] = f"{signal['from_state']} -> {signal['to_state']}"
            signal["detail"] = _elite_trigger_text(signal)
            latest_signal_by_symbol[signal["symbol"]] = signal
            events.append(signal)
            continue

        if rec := RECONCILE_RE.search(message):
            state = {
                "ts": parsed["ts"],
                "level": parsed["level"],
                "type": "position_update",
                "symbol": rec.group("symbol"),
                "side": rec.group("side"),
                "state": rec.group("state"),
                "entry": float(rec.group("entry")),
                "price": float(rec.group("price")),
                "pnl_pct": float(rec.group("pnl_pct")),
                "held_min": int(rec.group("held_min")),
                "flow": float(rec.group("flow")),
                "crowd": float(rec.group("crowd")),
                "regime": float(rec.group("regime")),
                "leverage": leverage_by_symbol.get(rec.group("symbol"), DEFAULT_ELITE_LEVERAGE),
                "title": f"{rec.group('side').upper()} {rec.group('state')}",
                "detail": (
                    f"Entry {float(rec.group('entry')):,.2f}, mark {float(rec.group('price')):,.2f}, "
                    f"PnL {float(rec.group('pnl_pct')):+.2f}%, held {int(rec.group('held_min'))}m"
                ),
            }
            latest_position_by_symbol[state["symbol"]] = state
            continue

        if placed := PLACED_RE.search(message):
            symbol = placed.group("symbol")
            signal = latest_signal_by_symbol.get(symbol, {})
            side = placed.group("side")
            events.append({
                "ts": parsed["ts"],
                "level": parsed["level"],
                "type": "entry",
                "symbol": symbol,
                "side": side,
                "contracts": int(placed.group("contracts")),
                "leverage": leverage_by_symbol.get(symbol, DEFAULT_ELITE_LEVERAGE),
                "conviction": signal.get("conviction"),
                "flow": signal.get("flow"),
                "crowd": signal.get("crowd"),
                "regime": signal.get("regime"),
                "title": f"{side.upper()} {symbol}",
                "detail": signal.get("detail", "Entry order sent to OKX"),
            })
            continue

        if closed := CLOSED_RE.search(message):
            symbol = closed.group("symbol")
            position = latest_position_by_symbol.get(symbol, {})
            events.append({
                "ts": parsed["ts"],
                "level": parsed["level"],
                "type": "exit",
                "symbol": symbol,
                "side": position.get("side"),
                "pnl_pct": position.get("pnl_pct"),
                "held_min": position.get("held_min"),
                "leverage": position.get("leverage") or leverage_by_symbol.get(symbol, DEFAULT_ELITE_LEVERAGE),
                "title": f"EXIT {symbol}",
                "detail": position.get("detail", "Position close sent to OKX"),
            })

    events = events[-limit:]
    return {
        "events": events,
        "latest_signal_by_symbol": latest_signal_by_symbol,
        "latest_position_by_symbol": latest_position_by_symbol,
    }


def api_trade_summary(qs: Dict) -> Dict:
    limit = int(qs.get("limit", ["12"])[0])
    bills = api_bills({})
    decision = api_decision_journal({"limit": ["600"]})
    events = decision.get("events", [])

    grouped_orders: Dict[str, Dict[str, Any]] = {}
    for fill in bills:
        ord_id = fill.get("ordId") or ""
        if not ord_id:
            continue
        item = grouped_orders.setdefault(ord_id, {
            "ordId": ord_id,
            "time": fill.get("time"),
            "instId": fill.get("instId"),
            "side": fill.get("side"),
            "fillSz": 0.0,
            "notional": 0.0,
            "fee": 0.0,
            "pnl": 0.0,
            "fills": 0,
        })
        item["fillSz"] += float(fill.get("fillSz") or 0)
        item["notional"] += float(fill.get("notional") or 0)
        item["fee"] += float(fill.get("fee") or 0)
        item["pnl"] += float(fill.get("pnl") or 0)
        item["fills"] += 1
        if fill.get("time", "") > (item.get("time") or ""):
            item["time"] = fill.get("time")

    grouped_order_list = sorted(grouped_orders.values(), key=lambda x: x.get("time", ""), reverse=True)

    recent_trades: List[Dict[str, Any]] = []
    last_entry_by_symbol: Dict[str, Dict[str, Any]] = {}
    exits = []
    for event in events:
        etype = event.get("type")
        symbol = event.get("symbol")
        if not symbol:
            continue
        if etype == "entry":
            last_entry_by_symbol[symbol] = event
        elif etype == "exit":
            entry = last_entry_by_symbol.get(symbol)
            trade = {
                "symbol": symbol,
                "entry_ts": entry.get("ts") if entry else None,
                "exit_ts": event.get("ts"),
                "side": event.get("side"),
                "leverage": event.get("leverage") or (entry or {}).get("leverage"),
                "held_min": event.get("held_min"),
                "pnl_pct": event.get("pnl_pct"),
                "trigger": (entry or {}).get("detail"),
                "entry_title": (entry or {}).get("title"),
                "exit_detail": event.get("detail"),
            }
            recent_trades.append(trade)
            exits.append(trade)

    recent_trades = recent_trades[-limit:][::-1]

    pnl_values = [float(t.get("pnl_pct") or 0) for t in exits if t.get("pnl_pct") is not None]
    held_values = [int(t.get("held_min") or 0) for t in exits if t.get("held_min") is not None]
    wins = [v for v in pnl_values if v > 0]
    losses = [v for v in pnl_values if v < 0]
    metrics = {
        "closed_trades": len(pnl_values),
        "win_rate_pct": round((len(wins) / len(pnl_values) * 100.0), 1) if pnl_values else 0.0,
        "avg_pnl_pct": round(sum(pnl_values) / len(pnl_values), 3) if pnl_values else 0.0,
        "best_pnl_pct": round(max(pnl_values), 3) if pnl_values else 0.0,
        "worst_pnl_pct": round(min(pnl_values), 3) if pnl_values else 0.0,
        "avg_hold_min": round(sum(held_values) / len(held_values), 1) if held_values else 0.0,
        "gross_realized_pnl": round(sum(float(o.get("pnl") or 0) for o in grouped_order_list), 4),
        "total_fees": round(sum(float(o.get("fee") or 0) for o in grouped_order_list), 4),
    }

    return {
        "metrics": metrics,
        "recent_trades": recent_trades,
        "recent_orders": grouped_order_list[:limit],
    }


def api_backtest(qs: Dict) -> Dict:
    global _backtest_cache
    profile = qs.get("profile", ["daily"])[0]
    force   = qs.get("force", ["0"])[0] == "1"
    key     = profile

    with _backtest_lock:
        if not force and key in _backtest_cache:
            return _backtest_cache[key]

    try:
        result = subprocess.run(
            [sys.executable, "run_backtest.py",
             "--engine", "unified",
             "--profile", profile,
             "--quick-test",
             "--no-plot"],
            cwd=str(BASE_DIR),
            capture_output=True, text=True, timeout=180,
        )
        # Parse metrics lines from stdout
        metrics_raw = {}
        nav_data: List[Dict] = []
        for line in result.stdout.splitlines():
            if "│" in line or "─" in line or "═" in line:
                continue
            if "%" in line or "$" in line or "." in line:
                parts = [p.strip() for p in line.strip().split() if p.strip()]
                if len(parts) >= 2:
                    k = " ".join(parts[:-1])
                    v = parts[-1]
                    metrics_raw[k] = v

        # Also get performance CSV written during backtest (if any temp output)
        perf = read_csv_as_dicts(LOGS_DIR / "performance.csv")
        nav_data = [
            {"ts": r["ts"], "nav": float(r["nav"]), "portfolio": r["portfolio_id"]}
            for r in perf if r.get("nav")
        ]

        out = {
            "profile": profile,
            "stdout": result.stdout[-3000:],
            "nav_data": nav_data,
            "ran_at": datetime.now(timezone.utc).isoformat(),
            "error": result.stderr[-1000:] if result.returncode != 0 else None,
        }
        with _backtest_lock:
            _backtest_cache[key] = out
        return out
    except Exception as e:
        return {"error": str(e), "profile": profile}


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

def api_bills(qs: Dict) -> List[Dict]:
    """Fetch live fill history from OKX for all traded instruments."""
    instruments = qs.get("instId", [])
    if not instruments:
        instruments = [
            "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP",
            "BNB-USDT-SWAP", "ADA-USDT-SWAP", "AVAX-USDT-SWAP",
        ]

    CT_VAL = {
        "BTC-USDT-SWAP": 0.01, "ETH-USDT-SWAP": 0.1, "SOL-USDT-SWAP": 1.0,
        "BNB-USDT-SWAP": 0.01, "ADA-USDT-SWAP": 100.0, "AVAX-USDT-SWAP": 1.0,
    }

    all_fills: List[Dict] = []
    for inst in instruments:
        try:
            r = subprocess.run(
                ["okx", "--profile", get_okx_default_profile(), "--json", "swap", "fills", "--instId", inst],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode == 0 and r.stdout.strip():
                import json as _json
                fills = _json.loads(r.stdout)
                for f in (fills if isinstance(fills, list) else []):
                    # Convert epoch ms to ISO
                    ts_ms = int(f.get("ts") or f.get("fillTime") or 0)
                    ts_iso = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat() if ts_ms else ""
                    fill_px = float(f.get("fillPx", 0) or 0)
                    fill_sz = float(f.get("fillSz", 0) or 0)
                    ct_val = CT_VAL.get(f.get("instId", ""), 1.0)
                    notional = fill_sz * ct_val * fill_px

                    all_fills.append({
                        "time":       ts_iso,
                        "instId":     f.get("instId", ""),
                        "side":       f.get("side", ""),
                        "fillPx":     fill_px,
                        "fillSz":     fill_sz,
                        "notional":   round(notional, 2),
                        "fee":        float(f.get("fee", 0) or 0),
                        "pnl":        float(f.get("fillPnl", 0) or 0),
                        "ordId":      f.get("ordId", ""),
                        "tag":        f.get("tag", ""),
                    })
        except Exception:
            pass

    # Sort by time descending (newest first)
    all_fills.sort(key=lambda x: x.get("time", ""), reverse=True)
    return all_fills


def api_nav_history(_qs: Dict) -> List[Dict]:
    """NAV history — prefer yolo orchestrator state, fall back to nav_history.jsonl."""
    # Try yolo orchestrator first (it tracks per-slot nav_history)
    yolo = read_json(get_yolo_orchestrator_state_path())
    if yolo and yolo.get("slots"):
        slots_with_history = [
            s for s in yolo["slots"]
            if s.get("nav_history") and s.get("status") in ("running", "succeeded", "drained")
        ]
        if slots_with_history:
            # Merge all slot timelines into {ts -> {slot_id: nav}} entries
            ts_map: Dict[str, Dict[str, float]] = {}
            for slot in slots_with_history:
                key = f"slot_{slot.get('id', '?')}"
                for pt in slot["nav_history"]:
                    ts = pt.get("ts", "")
                    if ts not in ts_map:
                        ts_map[ts] = {}
                    ts_map[ts][key] = pt.get("nav", 0)

            # Build sorted rows, forward-filling missing slots
            sorted_ts = sorted(ts_map.keys())
            slot_keys = sorted({k for m in ts_map.values() for k in m})
            last_vals = {k: 0.0 for k in slot_keys}
            rows = []
            for ts in sorted_ts:
                entry = {"ts": ts}
                for k in slot_keys:
                    if k in ts_map[ts]:
                        last_vals[k] = ts_map[ts][k]
                    entry[k] = last_vals[k]
                entry["total"] = round(sum(entry[k] for k in slot_keys), 2)
                rows.append(entry)
            return rows[-2000:]

    # Fallback to session daemon's nav_history.jsonl
    nav_path = LOGS_DIR / "nav_history.jsonl"
    return read_jsonl_tail(nav_path, n=2000)


def api_nav_events(_qs: Dict) -> List[Dict]:
    """Extract trade events + capital deployments from yolo orchestrator for chart annotations."""
    yolo = read_json(get_yolo_orchestrator_state_path())
    if not yolo:
        return []

    events: List[Dict] = []
    for slot in yolo.get("slots", []):
        sid = slot.get("id", "?")
        status = slot.get("status", "")
        if status not in ("running", "succeeded", "drained"):
            continue

        started = slot.get("started_at")
        round_margins = [50, 100, 200, 400]
        trades = slot.get("trades", [])
        close_trades = [t for t in trades if t.get("action") == "close"]
        pos = slot.get("current_position")
        current_round = slot.get("round", 1)
        nh = slot.get("nav_history", [])

        def find_nav_jump(after_ts: str, threshold: float, positive_only: bool = False) -> str:
            """Find the nav_history timestamp where NAV jumps by >= threshold after after_ts."""
            for i in range(1, len(nh)):
                if nh[i]["ts"] < after_ts:
                    continue
                delta = nh[i].get("nav", 0) - nh[i-1].get("nav", 0)
                if positive_only and delta < threshold:
                    continue
                if not positive_only and abs(delta) < threshold:
                    continue
                return nh[i]["ts"]
            return after_ts

        # Round 1 capital at slot start
        if started:
            events.append({
                "ts": started,
                "type": "capital",
                "label": f"Slot {sid}: +${round_margins[0]} deployed",
                "color": "#58a6ff",
            })

        # For each closed trade: infer entry before it, then exit, then top-up for next round
        for trade in close_trades:
            ts = trade.get("ts")
            if not ts:
                continue
            inst = trade.get("inst_id", "").replace("-USDT-SWAP", "")
            side = trade.get("side", "?")
            pnl = trade.get("pnl", 0)
            reason = trade.get("reason", "")
            rd = trade.get("round", 1)

            # Infer entry time — round 1 at start, later rounds at positive NAV jump (capital injection)
            if rd == 1:
                entry_ts = started
            else:
                prev = [t for t in close_trades if t.get("round", 1) < rd]
                prev_ts = prev[-1].get("ts", started) if prev else started
                entry_ts = find_nav_jump(prev_ts, 10, positive_only=True)

            events.append({
                "ts": entry_ts,
                "type": "entry",
                "label": f"Slot {sid}: {side} {inst}",
                "color": "#3fb950" if side == "long" else "#f85149",
            })

            pnl_str = f"+${pnl:.1f}" if pnl >= 0 else f"-${abs(pnl):.1f}"
            events.append({
                "ts": ts,
                "type": "exit",
                "label": f"Slot {sid}: exit {inst} {pnl_str} ({reason})",
                "color": "#3fb950" if pnl >= 0 else "#f85149",
            })

            # Capital top-up for next round — align to actual positive NAV jump
            next_margin_idx = rd
            if next_margin_idx < len(round_margins) and next_margin_idx < current_round:
                jump_ts = find_nav_jump(ts, round_margins[next_margin_idx] * 0.5, positive_only=True)
                events.append({
                    "ts": jump_ts,
                    "type": "capital",
                    "label": f"Slot {sid}: top-up +${round_margins[next_margin_idx]}",
                    "color": "#58a6ff",
                })

        # Current open position entry — align to NAV jump after last close
        if pos and pos.get("inst_id"):
            inst = pos["inst_id"].replace("-USDT-SWAP", "")
            side = pos.get("side", "?")
            lev = pos.get("leverage", "?")
            if close_trades:
                entry_ts = find_nav_jump(close_trades[-1].get("ts", started), 10, positive_only=True)
            else:
                entry_ts = started
            events.append({
                "ts": entry_ts,
                "type": "entry",
                "label": f"Slot {sid}: {side} {inst} {lev}x",
                "color": "#3fb950" if side == "long" else "#f85149",
            })

        # Slot finished
        finished = slot.get("finished_at")
        if finished and status == "succeeded":
            total_pnl = slot.get("total_pnl", 0)
            pnl_str = f"+${total_pnl:.1f}" if total_pnl >= 0 else f"-${abs(total_pnl):.1f}"
            events.append({
                "ts": finished,
                "type": "slot_done",
                "label": f"Slot {sid}: done {pnl_str}",
                "color": "#3fb950" if total_pnl >= 0 else "#f85149",
            })

    events.sort(key=lambda x: x.get("ts", ""))
    return events


CT_VAL_MAP = {
    "BTC-USDT-SWAP": 0.01, "ETH-USDT-SWAP": 0.1, "SOL-USDT-SWAP": 1.0,
    "BNB-USDT-SWAP": 0.01, "ADA-USDT-SWAP": 100.0, "AVAX-USDT-SWAP": 1.0,
    "THETA-USDT-SWAP": 10.0, "DOGE-USDT-SWAP": 1000.0,
}
ROUND_MARGINS = [50, 100, 200, 400]


def api_yolo(_qs: Dict) -> Dict:
    """YOLO dashboard data.

    Prefer the freshest active runtime:
    1. live yolo_momentum state
    2. demo yolo_momentum state
    3. orchestrator summary
    """
    orchestrator_path = get_yolo_orchestrator_state_path()
    live_state_path = LOGS_DIR / "yolo_momentum_live_state.json"
    demo_state_path = LOGS_DIR / "yolo_momentum_state.json"

    candidates: List[tuple[float, str, Path]] = []
    if live_state_path.exists():
        candidates.append((live_state_path.stat().st_mtime, "live", live_state_path))
    if demo_state_path.exists():
        candidates.append((demo_state_path.stat().st_mtime, "demo", demo_state_path))
    if orchestrator_path.exists():
        candidates.append((orchestrator_path.stat().st_mtime, "orchestrator", orchestrator_path))

    data: Dict[str, Any]
    if candidates:
        _mtime, mode, chosen = max(candidates, key=lambda item: item[0])
        if mode in ("live", "demo"):
            data = _build_momentum_dashboard_data(chosen, mode)
        else:
            data = read_json(chosen) or {}
    else:
        data = {}

    if not data:
        data = {
            "updated_at": None, "total_budget": 10000, "total_deployed": 0,
            "total_succeeded": 0, "total_drained": 0, "total_running": 0,
            "total_pending": 10, "overall_roi_pct": 0, "overall_pnl": 0,
            "overall_invested": 0, "slots": [],
        }
    data = _reconcile_yolo_with_exchange(data)
    # Backfill margin/fees on trades missing them
    for slot in data.get("slots", []):
        for t in slot.get("trades", []):
            if t.get("margin") is not None:
                continue
            inst = t.get("inst_id", "")
            ct_val = CT_VAL_MAP.get(inst, 1.0)
            sz = t.get("capital_deployed", 0)
            rd = t.get("round", 1)
            lev = t.get("leverage") or 50  # default
            # Infer entry price from PnL and round margin
            margin_budget = ROUND_MARGINS[rd - 1] if rd <= len(ROUND_MARGINS) else 50
            entry_px = (sz * ct_val) and (margin_budget * lev) / (sz * ct_val) if sz * ct_val else 0
            notional = sz * ct_val * entry_px if entry_px else margin_budget * lev
            margin = notional / lev if lev else 0
            fee_est = notional * 0.0005 * 2
            t["leverage"] = lev
            t["margin"] = round(margin, 2)
            t["fee_est"] = round(fee_est, 4)
    return data


ROUTES = {
    "/api/yolo":           api_yolo,
    "/api/live":           api_live,
    "/api/account":        api_account,
    "/api/decision_journal": api_decision_journal,
    "/api/trade_summary":  api_trade_summary,
    "/api/performance":    api_performance,
    "/api/trades":         api_trades,
    "/api/signals":        api_signals,
    "/api/risk":           api_risk,
    "/api/model_signals":  api_model_signals,
    "/api/backtest":       api_backtest,
    "/api/bills":          api_bills,
    "/api/nav_history":    api_nav_history,
    "/api/nav_events":     api_nav_events,
}


class Handler(BaseHTTPRequestHandler):

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/") or "/"
        qs     = parse_qs(parsed.query)

        # API routes
        if path in ROUTES:
            data = ROUTES[path](qs)
            body = json.dumps(data, default=str).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
            return

        # Static files
        if path == "/":
            static = STATIC_DIR / "index.html"
        elif path == "/yolo":
            static = STATIC_DIR / "yolo.html"
        else:
            static = STATIC_DIR / path.lstrip("/")

        if static.exists() and static.is_file():
            ct = {
                ".html": "text/html",
                ".js":   "application/javascript",
                ".css":  "text/css",
            }.get(static.suffix, "application/octet-stream")
            body = static.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *_):
        pass  # suppress access log


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()

    url = f"http://localhost:{args.port}"
    print(f"Dashboard → {url}  (Ctrl+C to stop)")

    if args.open:
        subprocess.Popen(["open", url])

    ThreadingHTTPServer(("", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
