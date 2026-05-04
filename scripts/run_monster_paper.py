#!/usr/bin/env python3
"""Paper-trading loop for the monster-coin strategy.

The script reads the latest monster watchlist, simulates positions, and
persists state. It never submits orders to OKX.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from monster_live_gates import LiveGateConfig, build_live_gate_table  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
OUT_ROOT = ROOT / "engine" / "data" / "monster_events"
PAPER_DIR = ROOT / "engine" / "logs" / "monster_paper"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run monster strategy in paper mode")
    p.add_argument("--state-id", default="default")
    p.add_argument("--initial-capital", type=float, default=1000.0)
    p.add_argument("--mode", choices=["simple", "lottery"], default="simple")
    p.add_argument("--capital-per-trade", type=float, default=0.15)
    p.add_argument("--max-positions", type=int, default=3)
    p.add_argument("--score-threshold", type=float, default=0.75)
    p.add_argument("--risk-budget", type=float, default=20.0)
    p.add_argument("--max-open-risk", type=float, default=60.0)
    p.add_argument("--leverage", type=float, default=5.0)
    p.add_argument("--stop-loss", type=float, default=0.10)
    p.add_argument("--take-profit", type=float, default=0.25)
    p.add_argument("--tp1", type=float, default=0.50)
    p.add_argument("--tp1-fraction", type=float, default=0.35)
    p.add_argument("--tp2", type=float, default=1.50)
    p.add_argument("--tp2-fraction", type=float, default=0.35)
    p.add_argument("--trailing-stop", type=float, default=0.12)
    p.add_argument("--runner-trailing", type=float, default=0.30)
    p.add_argument("--max-hold-hours", type=float, default=72.0)
    p.add_argument("--disable-quality-exit", action="store_true")
    p.add_argument("--quality-exit-min-hours", type=float, default=4.0)
    p.add_argument("--exit-score-threshold", type=float, default=0.80)
    p.add_argument("--exit-ret-1h-threshold", type=float, default=-0.035)
    p.add_argument("--live-gate-exit-min-hours", type=float, default=2.0)
    p.add_argument("--enable-shorts", action="store_true")
    p.add_argument("--short-score", type=float, default=0.88)
    p.add_argument("--short-pump-24h", type=float, default=0.60)
    p.add_argument("--short-break-1h", type=float, default=-0.08)
    p.add_argument("--use-live-gates", action="store_true")
    p.add_argument("--live-max-age-minutes", type=float, default=180.0)
    p.add_argument("--live-max-spread-bps", type=float, default=20.0)
    p.add_argument("--live-min-depth-1pct-usd", type=float, default=10_000.0)
    p.add_argument("--live-min-oi-value", type=float, default=1_000_000.0)
    p.add_argument("--live-max-abs-funding-rate", type=float, default=0.0015)
    p.add_argument("--live-max-long-short-ratio", type=float, default=4.0)
    p.add_argument("--live-min-long-short-ratio", type=float, default=0.25)
    p.add_argument("--loop", action="store_true", help="Run continuously instead of a single cycle")
    p.add_argument("--interval-sec", type=float, default=300.0)
    p.add_argument("--refresh", action="store_true", help="Refresh public data before each paper cycle")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    PAPER_DIR.mkdir(parents=True, exist_ok=True)
    while True:
        if args.refresh:
            subprocess.run([sys.executable, "scripts/run_monster_refresh_and_score.py"], cwd=str(ROOT), check=False)
        state = _cycle(args)
        print(json.dumps(_summary(state), indent=2, sort_keys=True, default=str))
        if not args.loop:
            return 0
        time.sleep(max(30.0, args.interval_sec))


def _cycle(args: argparse.Namespace) -> dict[str, Any]:
    state_path = PAPER_DIR / f"{args.state_id}.json"
    ledger_path = PAPER_DIR / f"{args.state_id}_ledger.jsonl"
    equity_path = PAPER_DIR / f"{args.state_id}_equity.jsonl"
    state = _load_state(state_path, args.initial_capital)
    now = pd.Timestamp.utcnow()
    watchlist = _latest_watchlist()
    prices = _latest_prices(watchlist)
    live_gates = _live_gates(args, now) if args.use_live_gates else {}

    _mark_and_exit(state, prices, now, args, ledger_path)
    _manage_position_quality(state, watchlist, prices, live_gates, now, args, ledger_path)
    _enter_candidates(state, watchlist, prices, live_gates, now, args, ledger_path)

    state["updated_at"] = now.isoformat()
    state["live_gates_enabled"] = bool(args.use_live_gates)
    state["live_gate_pass_count"] = sum(1 for item in live_gates.values() if int(item.get("live_gate_flag") or 0) == 1)
    state["nav"] = _nav(state, prices)
    state["unrealized_pnl"] = state["nav"] - state["cash"] - _reserved_capital(state)
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True, default=str))
    _append_equity(equity_path, state, now)
    return state


def _load_state(path: Path, initial_capital: float) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text())
    return {"cash": float(initial_capital), "nav": float(initial_capital), "positions": {}, "cooldown_until": {}}


def _latest_watchlist() -> pd.DataFrame:
    candidates = [
        p
        for p in OUT_ROOT.iterdir()
        if p.is_dir() and p.name.startswith("monster_") and (p / "watchlist.parquet").exists()
    ]
    if not candidates:
        raise SystemExit("No monster watchlist found. Run scripts/run_monster_refresh_and_score.py first.")
    run_dir = max(candidates, key=lambda p: (p / "watchlist.parquet").stat().st_mtime)
    return pd.read_parquet(run_dir / "watchlist.parquet").sort_values("monster_score_adj", ascending=False)


def _latest_prices(watchlist: pd.DataFrame) -> dict[str, float]:
    prices: dict[str, float] = {}
    for row in watchlist.to_dict(orient="records"):
        sym = row.get("symbol")
        price = row.get("last") or row.get("close")
        if sym and price and not pd.isna(price):
            prices[str(sym)] = float(price)
    return prices


def _mark_and_exit(
    state: dict[str, Any],
    prices: dict[str, float],
    now: pd.Timestamp,
    args: argparse.Namespace,
    ledger_path: Path,
) -> None:
    for sym, pos in list(state["positions"].items()):
        price = prices.get(sym)
        if price is None:
            continue
        if pos.get("mode") == "lottery":
            _mark_and_exit_lottery_position(state, sym, pos, price, now, args, ledger_path)
            continue
        pos["peak_price"] = max(float(pos.get("peak_price", pos["entry_price"])), price)
        stop = max(float(pos["entry_price"]) * (1.0 - args.stop_loss), pos["peak_price"] * (1.0 - args.trailing_stop))
        entry_ts = pd.Timestamp(pos["entry_ts"])
        reason = None
        if price <= stop:
            reason = "stop_or_trailing"
        elif price >= float(pos["entry_price"]) * (1.0 + args.take_profit):
            reason = "take_profit"
        elif now - entry_ts >= pd.Timedelta(hours=args.max_hold_hours):
            reason = "time_exit"
        if not reason:
            continue
        qty = float(pos["qty"])
        proceeds = qty * price
        pnl = proceeds - float(pos["notional"])
        state["cash"] += proceeds
        state["positions"].pop(sym)
        state["cooldown_until"][sym] = (now + pd.Timedelta(hours=24)).isoformat()
        _append_ledger(ledger_path, {"ts": now.isoformat(), "event": "exit", "symbol": sym, "price": price, "pnl": pnl, "reason": reason})


def _enter_candidates(
    state: dict[str, Any],
    watchlist: pd.DataFrame,
    prices: dict[str, float],
    live_gates: dict[str, dict[str, Any]],
    now: pd.Timestamp,
    args: argparse.Namespace,
    ledger_path: Path,
) -> None:
    slots = max(0, args.max_positions - len(state["positions"]))
    if slots <= 0:
        return
    open_risk = sum(float(pos.get("risk_budget", 0.0)) for pos in state["positions"].values())
    for row in watchlist.to_dict(orient="records"):
        sym = str(row.get("symbol"))
        if sym in state["positions"]:
            continue
        if pd.Timestamp(state["cooldown_until"].get(sym, "1970-01-01T00:00:00Z")) > now:
            continue
        if int(row.get("trade_candidate_flag") or 0) != 1:
            continue
        if float(row.get("monster_score_adj") or 0.0) < args.score_threshold:
            continue
        if args.use_live_gates and not _passes_live_gate(row, live_gates, now, ledger_path):
            continue
        price = prices.get(sym)
        if not price:
            continue
        if args.mode == "lottery":
            if open_risk + args.risk_budget > args.max_open_risk:
                return
            entered = _enter_lottery_candidate(state, row, price, now, args, ledger_path)
            if not entered:
                continue
            open_risk += args.risk_budget
            slots -= 1
            if slots <= 0:
                return
            continue
        notional = min(float(state["cash"]) * 0.95, float(state["nav"]) * args.capital_per_trade)
        if notional <= 1.0:
            return
        qty = notional / price
        state["cash"] -= notional
        state["positions"][sym] = {
            "entry_ts": now.isoformat(),
            "entry_price": price,
            "qty": qty,
            "notional": notional,
            "mode": "simple",
            "side": "long",
            "peak_price": price,
            "score": float(row.get("monster_score_adj") or 0.0),
            "trigger_reasons": row.get("trigger_reasons", ""),
        }
        _append_ledger(ledger_path, {"ts": now.isoformat(), "event": "entry", "symbol": sym, "price": price, "notional": notional})
        slots -= 1
        if slots <= 0:
            return


def _manage_position_quality(
    state: dict[str, Any],
    watchlist: pd.DataFrame,
    prices: dict[str, float],
    live_gates: dict[str, dict[str, Any]],
    now: pd.Timestamp,
    args: argparse.Namespace,
    ledger_path: Path,
) -> None:
    if args.disable_quality_exit:
        return
    rows = {str(row.get("symbol")): row for row in watchlist.to_dict(orient="records")}
    for sym, pos in list(state["positions"].items()):
        price = prices.get(sym)
        if price is None:
            continue
        entry_ts = pd.Timestamp(pos["entry_ts"])
        age_hours = (now - entry_ts).total_seconds() / 3600.0
        if age_hours < float(args.quality_exit_min_hours):
            continue
        row = rows.get(sym)
        reason = None
        latest_score = None
        ret_1h = None
        trade_candidate = None
        if row is None:
            reason = "missing_from_latest_watchlist"
        else:
            latest_score = _float_or_none(row.get("monster_score_adj"))
            ret_1h = _float_or_none(row.get("ret_1h"))
            trade_candidate = int(row.get("trade_candidate_flag") or 0)
            if trade_candidate != 1:
                reason = "not_trade_candidate"
            elif latest_score is not None and latest_score < float(args.exit_score_threshold):
                reason = "score_decay"
            elif ret_1h is not None and ret_1h <= float(args.exit_ret_1h_threshold):
                reason = "short_term_breakdown"
        gate = live_gates.get(sym)
        if (
            reason is None
            and args.use_live_gates
            and gate is not None
            and age_hours >= float(args.live_gate_exit_min_hours)
            and int(gate.get("live_gate_flag") or 0) != 1
        ):
            reason = "live_gate_failed"
        if reason is None:
            continue
        if pos.get("mode") == "lottery":
            _close_lottery_position(
                state,
                sym,
                pos,
                price,
                now,
                reason,
                ledger_path,
                extra={
                    "age_hours": age_hours,
                    "latest_score": latest_score,
                    "ret_1h": ret_1h,
                    "trade_candidate_flag": trade_candidate,
                    "live_gate_reasons": gate.get("live_gate_reasons", "") if gate else "",
                },
            )
            continue
        qty = float(pos["qty"])
        proceeds = qty * price
        pnl = proceeds - float(pos["notional"])
        state["cash"] += proceeds
        state["positions"].pop(sym)
        state["cooldown_until"][sym] = (now + pd.Timedelta(hours=24)).isoformat()
        _append_ledger(
            ledger_path,
            {
                "ts": now.isoformat(),
                "event": "exit",
                "symbol": sym,
                "price": price,
                "pnl": pnl,
                "reason": reason,
                "age_hours": age_hours,
                "latest_score": latest_score,
                "ret_1h": ret_1h,
                "trade_candidate_flag": trade_candidate,
            },
        )


def _enter_lottery_candidate(
    state: dict[str, Any],
    row: dict[str, Any],
    price: float,
    now: pd.Timestamp,
    args: argparse.Namespace,
    ledger_path: Path,
) -> bool:
    side = _lottery_side(row, args)
    if side is None:
        return False
    risk_budget = float(args.risk_budget)
    stop_dist = max(float(args.stop_loss), 0.001)
    notional_by_risk = risk_budget / stop_dist
    notional_by_margin = risk_budget * max(float(args.leverage), 1.0)
    notional = min(notional_by_risk, notional_by_margin)
    initial_margin = notional / max(float(args.leverage), 1.0)
    if float(state["cash"]) < initial_margin:
        return False
    qty = notional / price
    stop_price = price * (1.0 - args.stop_loss) if side == "long" else price * (1.0 + args.stop_loss)
    tp1_price = price * (1.0 + args.tp1) if side == "long" else price * (1.0 - args.tp1)
    tp2_price = price * (1.0 + args.tp2) if side == "long" else price * (1.0 - args.tp2)
    sym = str(row.get("symbol"))
    state["cash"] -= initial_margin
    state["positions"][sym] = {
        "mode": "lottery",
        "side": side,
        "entry_ts": now.isoformat(),
        "entry_price": price,
        "qty": qty,
        "notional": notional,
        "initial_margin": initial_margin,
        "risk_budget": risk_budget,
        "stop_price": stop_price,
        "tp1_price": tp1_price,
        "tp2_price": tp2_price,
        "tp1_done": False,
        "tp2_done": False,
        "peak_favorable_price": price,
        "runner_stop_price": price,
        "score": float(row.get("monster_score_adj") or 0.0),
        "trigger_reasons": row.get("trigger_reasons", ""),
        "live_gate_warnings": row.get("live_gate_warnings", ""),
        "live_orderbook_run": row.get("live_orderbook_run"),
        "live_derivatives_run": row.get("live_derivatives_run"),
        "live_spread_bps": row.get("live_spread_bps"),
        "live_depth_1pct_usd": row.get("live_depth_1pct_usd"),
        "live_open_interest_value": row.get("live_open_interest_value"),
        "live_funding_rate": row.get("live_funding_rate"),
        "live_long_short_ratio": row.get("live_long_short_ratio"),
    }
    _append_ledger(
        ledger_path,
        {
            "ts": now.isoformat(),
            "event": "entry",
            "mode": "lottery",
            "side": side,
            "symbol": sym,
            "price": price,
            "notional": notional,
            "initial_margin": initial_margin,
            "risk_budget": risk_budget,
            "live_orderbook_run": row.get("live_orderbook_run"),
            "live_derivatives_run": row.get("live_derivatives_run"),
            "live_spread_bps": row.get("live_spread_bps"),
            "live_depth_1pct_usd": row.get("live_depth_1pct_usd"),
            "live_open_interest_value": row.get("live_open_interest_value"),
            "live_funding_rate": row.get("live_funding_rate"),
            "live_long_short_ratio": row.get("live_long_short_ratio"),
        },
    )
    return True


def _lottery_side(row: dict[str, Any], args: argparse.Namespace) -> str | None:
    score = float(row.get("monster_score_adj") or 0.0)
    if args.enable_shorts:
        ret_24h = _float_or_none(row.get("ret_24h"))
        ret_1h = _float_or_none(row.get("ret_1h"))
        if (
            score >= args.short_score
            and ret_24h is not None
            and ret_24h >= args.short_pump_24h
            and ret_1h is not None
            and ret_1h <= args.short_break_1h
        ):
            return "short"
    if score >= args.score_threshold:
        return "long"
    return None


def _live_gates(args: argparse.Namespace, now: pd.Timestamp) -> dict[str, dict[str, Any]]:
    config = LiveGateConfig(
        max_snapshot_age_minutes=args.live_max_age_minutes,
        max_spread_bps=args.live_max_spread_bps,
        min_depth_1pct_usd=args.live_min_depth_1pct_usd,
        min_open_interest_value=args.live_min_oi_value,
        max_abs_funding_rate=args.live_max_abs_funding_rate,
        max_long_short_ratio=args.live_max_long_short_ratio,
        min_long_short_ratio=args.live_min_long_short_ratio,
    )
    table = build_live_gate_table(config, now=now)
    if table.empty:
        return {}
    return {str(row["symbol"]): row for row in table.to_dict(orient="records")}


def _passes_live_gate(
    row: dict[str, Any],
    live_gates: dict[str, dict[str, Any]],
    now: pd.Timestamp,
    ledger_path: Path,
) -> bool:
    sym = str(row.get("symbol"))
    gate = live_gates.get(sym)
    if not gate:
        _append_ledger(
            ledger_path,
            {
                "ts": now.isoformat(),
                "event": "live_gate_reject",
                "symbol": sym,
                "reason": "missing_live_gate_row",
                "score": row.get("monster_score_adj"),
            },
        )
        return False
    if int(gate.get("live_gate_flag") or 0) != 1:
        _append_ledger(
            ledger_path,
            {
                "ts": now.isoformat(),
                "event": "live_gate_reject",
                "symbol": sym,
                "reason": gate.get("live_gate_reasons", "live_gate_failed"),
                "warnings": gate.get("live_gate_warnings", ""),
                "score": row.get("monster_score_adj"),
            },
        )
        return False
    row.update(
        {
            "live_gate_flag": 1,
            "live_gate_warnings": gate.get("live_gate_warnings", ""),
            "live_orderbook_run": gate.get("ob_source_run_id"),
            "live_derivatives_run": gate.get("deriv_source_run_id"),
            "live_spread_bps": gate.get("ob_spread_bps"),
            "live_depth_1pct_usd": gate.get("ob_depth_1pct_usd"),
            "live_open_interest_value": gate.get("deriv_open_interest_value"),
            "live_funding_rate": gate.get("deriv_funding_rate"),
            "live_long_short_ratio": gate.get("deriv_long_short_ratio"),
        }
    )
    return True


def _mark_and_exit_lottery_position(
    state: dict[str, Any],
    sym: str,
    pos: dict[str, Any],
    price: float,
    now: pd.Timestamp,
    args: argparse.Namespace,
    ledger_path: Path,
) -> None:
    side = str(pos.get("side", "long"))
    entry = float(pos["entry_price"])
    favorable = price
    if side == "long":
        pos["peak_favorable_price"] = max(float(pos.get("peak_favorable_price", entry)), favorable)
        hard_stop_hit = price <= float(pos.get("stop_price", entry * (1.0 - args.stop_loss)))
        tp1_hit = price >= float(pos.get("tp1_price", entry * (1.0 + args.tp1)))
        tp2_hit = price >= float(pos.get("tp2_price", entry * (1.0 + args.tp2)))
        trail = float(pos["peak_favorable_price"]) * (1.0 - args.runner_trailing)
        runner_stop = max(float(pos.get("stop_price", entry * (1.0 - args.stop_loss))), trail)
        runner_hit = price <= runner_stop
    else:
        pos["peak_favorable_price"] = min(float(pos.get("peak_favorable_price", entry)), favorable)
        hard_stop_hit = price >= float(pos.get("stop_price", entry * (1.0 + args.stop_loss)))
        tp1_hit = price <= float(pos.get("tp1_price", entry * (1.0 - args.tp1)))
        tp2_hit = price <= float(pos.get("tp2_price", entry * (1.0 - args.tp2)))
        trail = float(pos["peak_favorable_price"]) * (1.0 + args.runner_trailing)
        runner_stop = min(float(pos.get("stop_price", entry * (1.0 + args.stop_loss))), trail)
        runner_hit = price >= runner_stop
    pos["runner_stop_price"] = runner_stop

    if tp1_hit and not bool(pos.get("tp1_done")):
        _partial_lottery_exit(state, sym, pos, price, args.tp1_fraction, now, "tp1", ledger_path)
        pos["tp1_done"] = True
        pos["stop_price"] = entry
    if tp2_hit and not bool(pos.get("tp2_done")):
        _partial_lottery_exit(state, sym, pos, price, args.tp2_fraction, now, "tp2", ledger_path)
        pos["tp2_done"] = True

    entry_ts = pd.Timestamp(pos["entry_ts"])
    time_hit = now - entry_ts >= pd.Timedelta(hours=args.max_hold_hours)
    reason = None
    if hard_stop_hit:
        reason = "hard_stop"
    elif runner_hit:
        reason = "runner_stop"
    elif time_hit:
        reason = "time_exit"
    if reason:
        _close_lottery_position(state, sym, pos, price, now, reason, ledger_path)


def _partial_lottery_exit(
    state: dict[str, Any],
    sym: str,
    pos: dict[str, Any],
    price: float,
    fraction: float,
    now: pd.Timestamp,
    reason: str,
    ledger_path: Path,
) -> None:
    fraction = max(0.0, min(float(fraction), 1.0))
    qty = float(pos.get("qty", 0.0)) * fraction
    if qty <= 0:
        return
    pnl = _position_pnl(str(pos.get("side", "long")), float(pos["entry_price"]), price, qty)
    margin_release = float(pos.get("initial_margin", 0.0)) * fraction
    state["cash"] += margin_release + pnl
    pos["qty"] = float(pos.get("qty", 0.0)) - qty
    pos["notional"] = float(pos.get("notional", 0.0)) * (1.0 - fraction)
    pos["initial_margin"] = float(pos.get("initial_margin", 0.0)) * (1.0 - fraction)
    _append_ledger(
        ledger_path,
        {
            "ts": now.isoformat(),
            "event": "partial_exit",
            "mode": "lottery",
            "reason": reason,
            "symbol": sym,
            "side": pos.get("side", "long"),
            "price": price,
            "qty": qty,
            "pnl": pnl,
        },
    )


def _close_lottery_position(
    state: dict[str, Any],
    sym: str,
    pos: dict[str, Any],
    price: float,
    now: pd.Timestamp,
    reason: str,
    ledger_path: Path,
    extra: dict[str, Any] | None = None,
) -> None:
    qty = float(pos.get("qty", 0.0))
    pnl = _position_pnl(str(pos.get("side", "long")), float(pos["entry_price"]), price, qty)
    state["cash"] += float(pos.get("initial_margin", 0.0)) + pnl
    state["positions"].pop(sym, None)
    state["cooldown_until"][sym] = (now + pd.Timedelta(hours=24)).isoformat()
    payload = {
        "ts": now.isoformat(),
        "event": "exit",
        "mode": "lottery",
        "reason": reason,
        "symbol": sym,
        "side": pos.get("side", "long"),
        "price": price,
        "qty": qty,
        "pnl": pnl,
        "return_on_risk_budget": pnl / float(pos.get("risk_budget", 1.0)),
    }
    if extra:
        payload.update(extra)
    _append_ledger(ledger_path, payload)


def _nav(state: dict[str, Any], prices: dict[str, float]) -> float:
    nav = float(state["cash"])
    for sym, pos in state["positions"].items():
        mark = prices.get(sym, float(pos["entry_price"]))
        if pos.get("mode") == "lottery":
            nav += float(pos.get("initial_margin", 0.0)) + _position_pnl(str(pos.get("side", "long")), float(pos["entry_price"]), mark, float(pos.get("qty", 0.0)))
        else:
            nav += float(pos["qty"]) * mark
    return nav


def _summary(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "updated_at": state.get("updated_at"),
        "cash": state.get("cash"),
        "nav": state.get("nav"),
        "positions": list(state.get("positions", {}).keys()),
        "open_risk": sum(float(pos.get("risk_budget", 0.0)) for pos in state.get("positions", {}).values()),
        "live_gates_enabled": state.get("live_gates_enabled", False),
        "live_gate_pass_count": state.get("live_gate_pass_count", 0),
    }


def _reserved_capital(state: dict[str, Any]) -> float:
    total = 0.0
    for pos in state.get("positions", {}).values():
        if pos.get("mode") == "lottery":
            total += float(pos.get("initial_margin", 0.0))
        else:
            total += float(pos.get("notional", 0.0))
    return total


def _position_pnl(side: str, entry: float, price: float, qty: float) -> float:
    return (price - entry) * qty if side == "long" else (entry - price) * qty


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        if pd.isna(value):
            return None
        return float(value)
    except Exception:
        return None


def _append_ledger(path: Path, payload: dict[str, Any]) -> None:
    with path.open("a") as f:
        f.write(json.dumps(payload, sort_keys=True, default=str) + "\n")


def _append_equity(path: Path, state: dict[str, Any], now: pd.Timestamp) -> None:
    positions = state.get("positions", {})
    row = {
        "ts": now.isoformat(),
        "cash": state.get("cash"),
        "nav": state.get("nav"),
        "unrealized_pnl": state.get("unrealized_pnl"),
        "open_risk": sum(float(pos.get("risk_budget", 0.0)) for pos in positions.values()),
        "position_count": len(positions),
        "positions": ",".join(sorted(positions)),
        "live_gates_enabled": state.get("live_gates_enabled", False),
        "live_gate_pass_count": state.get("live_gate_pass_count", 0),
    }
    with path.open("a") as f:
        f.write(json.dumps(row, sort_keys=True, default=str) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
