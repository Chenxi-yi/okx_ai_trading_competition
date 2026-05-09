#!/usr/bin/env python3
"""Evaluate the 8-layer production pipeline status from local artifacts."""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
ENGINE_DIR = ROOT / "engine"
POLICY_PATH = ENGINE_DIR / "config" / "pipeline_policy.json"
DATA_REFRESH_STATUS = ENGINE_DIR / "logs" / "data_refresh" / "status.json"
C_AUTO_PAPER_DIR = ENGINE_DIR / "logs" / "c_auto_v2_paper"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Evaluate 8-layer pipeline readiness")
    p.add_argument("--strategy-id", default="c_auto_v2_fixed1000_conservative")
    p.add_argument("--state-id", default="fixed1000_conservative")
    p.add_argument("--environment", default="competition")
    p.add_argument("--json", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    policy = _read_json(POLICY_PATH) or {}
    paper_state = _read_json(C_AUTO_PAPER_DIR / f"{args.state_id}_{args.environment}.json") or {}
    scheduler = _read_json(C_AUTO_PAPER_DIR / f"{args.state_id}_{args.environment}_scheduler.json") or {}
    data_refresh = _read_json(DATA_REFRESH_STATUS) or {}
    ledger = _read_jsonl(C_AUTO_PAPER_DIR / f"{args.state_id}_{args.environment}_ledger.jsonl")
    equity = _read_jsonl(C_AUTO_PAPER_DIR / f"{args.state_id}_{args.environment}_equity.jsonl")
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "strategy_id": args.strategy_id,
        "capital": policy.get("capital", {}),
        "environment_order": policy.get("environment_order", []),
        "layers": [
            _layer_data(data_refresh),
            _layer_research(args.strategy_id),
            _layer_evaluation(args.strategy_id),
            _layer_paper(paper_state, scheduler, policy),
            _layer_competition(policy, paper_state, ledger, equity),
            _layer_personal(policy),
            _layer_committee(paper_state),
            _layer_position_review(paper_state),
        ],
    }
    report["summary"] = {
        "passed_layers": sum(1 for layer in report["layers"] if layer["status"] == "pass"),
        "blocked_layers": sum(1 for layer in report["layers"] if layer["status"] == "block"),
        "missing_layers": sum(1 for layer in report["layers"] if layer["status"] == "missing"),
    }
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_text(report)
    return 0


def _layer_data(status: dict[str, Any]) -> dict[str, Any]:
    scheduler_status = str(status.get("scheduler_status") or "")
    failed = int(status.get("failed") or 0)
    ok_count = int(status.get("ok") or 0)
    last = status.get("last_record") or {}
    fresh = bool(last.get("fresh", False))
    completed_clean_cycle = ok_count > 0 and failed == 0 and not last
    ok = scheduler_status == "running" and failed == 0 and (fresh or completed_clean_cycle)
    return {
        "id": 1,
        "name": "automated_data_update",
        "status": "pass" if ok else "block",
        "evidence": {
            "scheduler_status": scheduler_status,
            "ok": ok_count,
            "failed": failed,
            "last_symbol": last.get("symbol"),
            "last_timeframe": last.get("timeframe"),
            "fresh": fresh,
            "completed_clean_cycle": completed_clean_cycle,
            "cache_after": last.get("cache_after"),
        },
        "next_action": None if ok else "fix data_refresh freshness/failures before promotion",
    }


def _layer_research(strategy_id: str) -> dict[str, Any]:
    paths = [
        ROOT / ".claude" / "knowledge" / "strategies" / "c_auto_v2.md",
        ENGINE_DIR / "strategies" / "specs" / "c_auto_v2_regime_policy.json",
    ]
    exists = [str(path.relative_to(ROOT)) for path in paths if path.exists()]
    ok = len(exists) == len(paths)
    return {
        "id": 2,
        "name": "automated_strategy_research",
        "status": "pass" if ok else "missing",
        "evidence": {"strategy_id": strategy_id, "artifacts": exists},
        "next_action": None if ok else "write missing strategy hypothesis/spec artifacts",
    }


def _layer_evaluation(strategy_id: str) -> dict[str, Any]:
    roots = [
        ENGINE_DIR / "data" / "research" / "c_auto",
        ENGINE_DIR / "results",
    ]
    artifacts: list[str] = []
    for root in roots:
        if not root.exists():
            continue
        artifacts.extend(str(path.relative_to(ROOT)) for path in root.glob("**/summary.*"))
    ok = bool(artifacts)
    return {
        "id": 3,
        "name": "automated_strategy_evaluation",
        "status": "pass" if ok else "missing",
        "evidence": {"strategy_id": strategy_id, "summary_artifacts": artifacts[-8:]},
        "next_action": None if ok else "run/register backtest and walk-forward evaluation artifacts",
    }


def _layer_paper(state: dict[str, Any], scheduler: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    gates = (policy.get("promotion_gates") or {}).get("paper_to_competition", {})
    metrics = state.get("metrics") or {}
    freshness = state.get("freshness") or {}
    max_dd = abs(float(metrics.get("max_drawdown") or 0.0))
    realized_pnl = float(state.get("realized_pnl") or 0.0)
    scheduler_ok = str(scheduler.get("scheduler_status") or "") == "running" and not scheduler.get("last_error")
    freshness_ok = bool(freshness.get("passed", False))
    drawdown_ok = max_dd <= float(gates.get("max_drawdown_pct", 0.03))
    capital_target = float((policy.get("capital") or {}).get("base_capital_usdt") or 0.0)
    initial_nav = float(metrics.get("initial_nav") or state.get("realized_nav") or 0.0)
    capital_ok = capital_target <= 0 or math.isclose(initial_nav, capital_target, rel_tol=0.001, abs_tol=1.0)
    ok = bool(state.get("available")) and scheduler_ok and freshness_ok and drawdown_ok and capital_ok
    reasons = []
    if not scheduler_ok:
        reasons.append("paper scheduler not healthy")
    if not freshness_ok:
        reasons.append("paper freshness failed")
    if not drawdown_ok:
        reasons.append("paper drawdown over gate")
    if not capital_ok:
        reasons.append("paper capital basis not aligned")
    return {
        "id": 4,
        "name": "paper_trading",
        "status": "pass" if ok else "block",
        "evidence": {
            "available": bool(state.get("available")),
            "scheduler_status": scheduler.get("scheduler_status"),
            "last_error": scheduler.get("last_error"),
            "initial_nav": initial_nav,
            "target_capital_usdt": capital_target,
            "nav": state.get("nav"),
            "realized_pnl": realized_pnl,
            "max_drawdown_abs": max_dd,
            "freshness_passed": freshness_ok,
            "open_positions": len(state.get("positions") or {}),
        },
        "next_action": None if ok else "; ".join(reasons),
    }


def _layer_competition(
    policy: dict[str, Any],
    state: dict[str, Any],
    ledger: list[dict[str, Any]],
    equity: list[dict[str, Any]],
) -> dict[str, Any]:
    gate = (policy.get("promotion_gates") or {}).get("paper_to_competition", {})
    gate_eval = _evaluate_paper_to_competition_gate(gate, state, ledger, equity)
    status = "missing" if gate_eval["passed"] else "block"
    return {
        "id": 5,
        "name": "competition_account_production",
        "status": status,
        "evidence": {
            "gate": gate,
            "paper_gate": gate_eval,
        },
        "next_action": (
            "paper gate passed; requires owner approval for competition small-capital live audit"
            if gate_eval["passed"]
            else "continue paper until all paper_to_competition gate checks pass"
        ),
    }


def _layer_personal(policy: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": 6,
        "name": "personal_account_production",
        "status": "missing",
        "evidence": {"gate": (policy.get("promotion_gates") or {}).get("competition_to_personal", {})},
        "next_action": "requires competition evidence and explicit owner approval",
    }


def _layer_committee(state: dict[str, Any]) -> dict[str, Any]:
    entries = [event for event in state.get("ledger_tail") or [] if event.get("event") == "entry"]
    audited = [event for event in entries if event.get("leverage_policy")]
    ok = not entries or len(audited) == len(entries)
    return {
        "id": 7,
        "name": "investment_committee",
        "status": "pass" if ok else "block",
        "evidence": {"entry_events": len(entries), "entry_events_with_leverage_policy": len(audited)},
        "next_action": None if ok else "ensure every approved entry carries leverage_policy audit metadata",
    }


def _layer_position_review(state: dict[str, Any]) -> dict[str, Any]:
    positions = state.get("positions") or {}
    missing_stop = [symbol for symbol, pos in positions.items() if pos.get("stop_price") is None]
    daily_review = C_AUTO_PAPER_DIR / "daily_review_scheduler.json"
    ok = not missing_stop and daily_review.exists()
    return {
        "id": 8,
        "name": "position_management_and_strategy_review",
        "status": "pass" if ok else "block",
        "evidence": {
            "open_positions": len(positions),
            "positions_missing_stop": missing_stop,
            "daily_review_scheduler_exists": daily_review.exists(),
        },
        "next_action": None if ok else "ensure all positions have stops and daily review scheduler is active",
    }


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    except Exception:
        return []
    return rows


def _evaluate_paper_to_competition_gate(
    gate: dict[str, Any],
    state: dict[str, Any],
    ledger: list[dict[str, Any]],
    equity: list[dict[str, Any]],
) -> dict[str, Any]:
    entries = [event for event in ledger if event.get("event") == "entry"]
    exits = [event for event in ledger if event.get("event") == "exit"]
    freshness_skips = [
        event for event in ledger
        if event.get("event") == "skip" and str(event.get("reason") or "").startswith("freshness_gate_failed:")
    ]
    stop_exits = [event for event in exits if event.get("reason") in {"stop", "target"}]
    stop_execution_rate = (len(stop_exits) / len(exits)) if exits else 1.0
    first_ts = _first_event_ts(equity, ledger, state)
    now_ts = datetime.now(timezone.utc)
    calendar_days = max(0.0, (now_ts - first_ts).total_seconds() / 86400.0) if first_ts else 0.0
    navs = [_float(row.get("nav")) for row in equity if _float(row.get("nav")) is not None]
    max_drawdown = _max_drawdown(navs)
    max_daily_loss = _max_daily_loss(equity, state)
    positions = state.get("positions") or {}
    leverages = [_float(pos.get("leverage")) or 0.0 for pos in positions.values()]
    max_leverage_seen = max(leverages + [_float(event.get("leverage")) or 0.0 for event in entries] + [0.0])
    nav = _float(state.get("nav")) or _float((state.get("metrics") or {}).get("initial_nav")) or 0.0
    open_risk = _float(state.get("open_risk")) or 0.0
    gross_leverage = (open_risk / nav) if nav > 0 else 0.0
    checks = [
        _check("min_calendar_days", calendar_days, float(gate.get("min_calendar_days", 0)), calendar_days >= float(gate.get("min_calendar_days", 0))),
        _check("min_closed_trades", len(exits), int(gate.get("min_closed_trades", 0)), len(exits) >= int(gate.get("min_closed_trades", 0))),
        _check("max_drawdown_pct", max_drawdown, float(gate.get("max_drawdown_pct", 1.0)), max_drawdown <= float(gate.get("max_drawdown_pct", 1.0))),
        _check("max_daily_loss_pct", max_daily_loss, float(gate.get("max_daily_loss_pct", 1.0)), max_daily_loss <= float(gate.get("max_daily_loss_pct", 1.0))),
        _check("min_stop_execution_rate", stop_execution_rate, float(gate.get("min_stop_execution_rate", 0.0)), stop_execution_rate >= float(gate.get("min_stop_execution_rate", 0.0))),
        _check("max_stale_data_events", len(freshness_skips), int(gate.get("max_stale_data_events", 0)), len(freshness_skips) <= int(gate.get("max_stale_data_events", 0))),
        _check("max_leverage", max_leverage_seen, float(gate.get("max_leverage", 1.0)), max_leverage_seen <= float(gate.get("max_leverage", 1.0))),
        _check("max_gross_leverage", gross_leverage, float(gate.get("max_gross_leverage", 1.0)), gross_leverage <= float(gate.get("max_gross_leverage", 1.0))),
    ]
    failed = [check for check in checks if not check["passed"]]
    return {
        "passed": not failed,
        "checks": checks,
        "failed_checks": [check["name"] for check in failed],
        "stats": {
            "calendar_days": calendar_days,
            "entry_events": len(entries),
            "closed_trades": len(exits),
            "stop_or_target_exits": len(stop_exits),
            "stop_execution_rate": stop_execution_rate,
            "stale_data_events": len(freshness_skips),
            "max_drawdown_pct": max_drawdown,
            "max_daily_loss_pct": max_daily_loss,
            "max_leverage_seen": max_leverage_seen,
            "gross_leverage": gross_leverage,
        },
    }


def _check(name: str, actual: Any, required: Any, passed: bool) -> dict[str, Any]:
    return {"name": name, "actual": actual, "required": required, "passed": bool(passed)}


def _first_event_ts(
    equity: list[dict[str, Any]],
    ledger: list[dict[str, Any]],
    state: dict[str, Any],
) -> datetime | None:
    candidates = [row.get("ts") for row in equity] + [event.get("ts") for event in ledger] + [state.get("timestamp")]
    parsed = [_parse_ts(ts) for ts in candidates if ts]
    parsed = [ts for ts in parsed if ts is not None]
    return min(parsed) if parsed else None


def _parse_ts(value: Any) -> datetime | None:
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _float(value: Any) -> float | None:
    try:
        out = float(value)
    except Exception:
        return None
    if not math.isfinite(out):
        return None
    return out


def _max_drawdown(navs: list[float]) -> float:
    if not navs:
        return 0.0
    peak = navs[0]
    max_dd = 0.0
    for nav in navs:
        peak = max(peak, nav)
        if peak > 0:
            max_dd = max(max_dd, 1.0 - nav / peak)
    return max_dd


def _max_daily_loss(equity: list[dict[str, Any]], state: dict[str, Any]) -> float:
    initial = _float((state.get("metrics") or {}).get("initial_nav")) or _float(state.get("realized_nav")) or 0.0
    if initial <= 0:
        return 0.0
    by_day: dict[str, list[float]] = {}
    for row in equity:
        ts = _parse_ts(row.get("ts"))
        nav = _float(row.get("nav"))
        if ts is None or nav is None:
            continue
        by_day.setdefault(ts.date().isoformat(), []).append(nav)
    losses = []
    for navs in by_day.values():
        if not navs:
            continue
        losses.append(max(0.0, (navs[0] - min(navs)) / initial))
    return max(losses) if losses else 0.0


def _print_text(report: dict[str, Any]) -> None:
    capital = report.get("capital") or {}
    print("8-Layer Pipeline Status")
    print(f"  capital_basis: {float(capital.get('base_capital_usdt') or 0):.2f} USDT")
    print(f"  monthly_target: {float(capital.get('monthly_return_target_pct') or 0):.1%}")
    for layer in report["layers"]:
        print(f"{layer['id']}. {layer['name']}: {layer['status']}")
        if layer.get("next_action"):
            print(f"   next: {layer['next_action']}")
        if layer["name"] == "competition_account_production":
            gate = (layer.get("evidence") or {}).get("paper_gate") or {}
            failed = gate.get("failed_checks") or []
            if failed:
                print(f"   failed_checks: {', '.join(failed)}")


if __name__ == "__main__":
    raise SystemExit(main())
