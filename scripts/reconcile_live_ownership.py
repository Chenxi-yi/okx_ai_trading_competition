#!/usr/bin/env python3
"""Reconcile accounting-owned live ownership with OKX exchange truth."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
ENGINE_DIR = ROOT / "engine"
sys.path.insert(0, str(ENGINE_DIR))

from accounting import LiveOwnershipJournal  # noqa: E402
from execution.reconciliation import AccountReconciler  # noqa: E402
from kit import AccountProbe, KitClient, KitClientConfig, KitCommand  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reconcile live ownership journal against OKX positions")
    parser.add_argument("--environment", choices=["personal", "competition"], required=True)
    parser.add_argument("--okx-profile", default="")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--write-status", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    profile = args.okx_profile or ("live" if args.environment == "competition" else args.environment)
    journal = LiveOwnershipJournal.from_engine_dir(ENGINE_DIR, args.environment, profile)
    owned = journal.rebuild_open_ownership()
    exchange_error = ""
    performance = _empty_performance(args.environment, profile)
    try:
        exchange_positions = _exchange_positions(profile)
        snapshot = AccountReconciler().reconcile_owned_live_positions(
            owned,
            exchange_positions,
            environment=args.environment,
            okx_profile=profile,
        )
        journal.append_reconciliation(snapshot)
        ok = snapshot.ok
        errors = list(snapshot.errors)
        checked_at = snapshot.checked_at.isoformat()
        performance = _performance_summary(journal, profile, exchange_positions, limit=int(args.limit))
    except Exception as exc:
        exchange_positions = {}
        exchange_error = str(exc)
        ok = False
        errors = ["exchange_read_failed"]
        checked_at = datetime.now(timezone.utc).isoformat()
    payload = {
        "ok": ok,
        "environment": args.environment,
        "okx_profile": profile,
        "checked_at": checked_at,
        "owned_count": len(owned),
        "exchange_count": len(exchange_positions),
        "errors": errors,
        "exchange_error": exchange_error,
        "owned_positions": owned,
        "exchange_positions": exchange_positions,
        "performance": performance,
    }
    if args.write_status:
        _write_status(args.environment, payload)
        _write_performance(args.environment, performance)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        status = "ok" if snapshot.ok else "mismatch"
        print(
            f"{status}: environment={args.environment} profile={profile} "
            f"owned={len(owned)} exchange={len(exchange_positions)} errors={len(snapshot.errors)}"
        )
    return 0 if ok else 1


def _exchange_positions(profile: str, attempts: int = 3) -> dict[str, dict[str, Any]]:
    probe = AccountProbe(KitClient(KitClientConfig(default_profile=profile)), profile=profile)
    rows = _retry_kit_read(lambda: probe.positions(inst_type="SWAP"), attempts=attempts)
    if isinstance(rows, dict):
        rows = [rows]
    out: dict[str, dict[str, Any]] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        inst_id = str(row.get("instId") or "")
        if not inst_id:
            continue
        try:
            size = abs(float(row.get("pos") or 0.0))
        except Exception:
            size = 0.0
        if size > 0:
            out[inst_id] = row
    return out


def _retry_kit_read(fn, *, attempts: int) -> Any:
    last_error: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return fn()
        except Exception as exc:
            last_error = exc
            if attempt >= attempts:
                break
            time.sleep(min(2.0 * attempt, 5.0))
    if last_error is not None:
        raise last_error
    return None


def _performance_summary(
    journal: LiveOwnershipJournal,
    profile: str,
    exchange_positions: dict[str, dict[str, Any]],
    *,
    limit: int,
) -> dict[str, Any]:
    events = list(journal.iter_events())
    plans: dict[str, dict[str, Any]] = {}
    executions: list[dict[str, Any]] = []
    closes: list[dict[str, Any]] = []
    for event in events:
        event_type = str(event.get("event") or "")
        if event_type == "approved_plan" and isinstance(event.get("plan"), dict):
            plan = dict(event["plan"])
            decision_id = str(plan.get("decision_id") or "")
            if decision_id:
                plans[decision_id] = plan
        elif event_type == "execution" and isinstance(event.get("receipt"), dict):
            executions.append(dict(event["receipt"]))
        elif event_type == "close":
            closes.append(event)

    inst_ids = sorted(
        {
            str(row.get("inst_id") or "")
            for row in executions
            if str(row.get("inst_id") or "")
        }
        | set(exchange_positions)
    )
    order_to_execution: dict[str, dict[str, Any]] = {}
    inst_to_strategy: dict[str, str] = {}
    for receipt in executions:
        decision_id = str(receipt.get("decision_id") or "")
        strategy_id = _strategy_for_decision(plans, decision_id)
        inst_id = str(receipt.get("inst_id") or "")
        if inst_id and strategy_id:
            inst_to_strategy.setdefault(inst_id, strategy_id)
        for order_id in _order_ids(receipt.get("order_ids")):
            order_to_execution[order_id] = {"receipt": receipt, "strategy_id": strategy_id}

    fills_by_inst = {inst_id: _swap_fills(profile, inst_id, limit) for inst_id in inst_ids}
    bills = _account_bills(profile, limit)
    strategies: dict[str, dict[str, Any]] = {}
    unmatched_fills = 0
    unmatched_bills = 0

    for receipt in executions:
        decision_id = str(receipt.get("decision_id") or "")
        strategy_id = _strategy_for_decision(plans, decision_id) or "unknown"
        row = _perf_row(strategies, strategy_id)
        row["submitted_orders"] += 1
        row["filled_contracts"] += _float(receipt.get("filled_contracts"))
        row["execution_fees_usdt"] += _float(receipt.get("fee_usdt"))

    for inst_id, fills in fills_by_inst.items():
        for fill in fills:
            order_id = str(fill.get("ordId") or "")
            match = order_to_execution.get(order_id)
            strategy_id = str((match or {}).get("strategy_id") or inst_to_strategy.get(inst_id) or "unknown")
            if strategy_id == "unknown":
                unmatched_fills += 1
            row = _perf_row(strategies, strategy_id)
            row["exchange_fills"] += 1
            row["filled_contracts"] += abs(_float(fill.get("fillSz")))
            row["exchange_fees_usdt"] += _float(fill.get("fee"))
            fill_pnl = _float(fill.get("fillPnl"))
            row["exchange_fill_pnl_usdt"] += fill_pnl
            net = fill_pnl + _float(fill.get("fee"))
            if fill_pnl != 0.0:
                row["closed_fills"] += 1
                if net > 0:
                    row["gross_wins"] += 1
                elif net < 0:
                    row["gross_losses"] += 1

    related_bills = 0
    for bill in bills:
        inst_id = str(bill.get("instId") or "")
        if not inst_ids or inst_id not in inst_ids:
            continue
        related_bills += 1
        strategy_id = inst_to_strategy.get(inst_id) or "unknown"
        if strategy_id == "unknown":
            unmatched_bills += 1
        row = _perf_row(strategies, strategy_id)
        row["exchange_bills"] += 1
        row["bill_pnl_usdt"] += _float(bill.get("pnl"))
        row["bill_fees_usdt"] += _float(bill.get("fee"))

    for close in closes:
        strategy_id = str(close.get("strategy_id") or "unknown")
        row = _perf_row(strategies, strategy_id)
        row["close_events"] += 1

    open_owned = journal.rebuild_open_ownership()
    for owned in open_owned.values():
        owners = owned.get("owners")
        if isinstance(owners, list) and owners:
            seen: set[str] = set()
            for owner in owners:
                if not isinstance(owner, dict):
                    continue
                strategy_id = str(owner.get("strategy_id") or "unknown")
                if strategy_id in seen:
                    continue
                seen.add(strategy_id)
                _perf_row(strategies, strategy_id)["open_positions"] += 1
        else:
            strategy_id = str(owned.get("strategy_id") or "unknown")
            _perf_row(strategies, strategy_id)["open_positions"] += 1

    rows = []
    for strategy_id, row in strategies.items():
        closed = int(row["closed_fills"])
        row["strategy_id"] = strategy_id
        row["net_pnl_usdt"] = row["exchange_fill_pnl_usdt"] + row["exchange_fees_usdt"] + row["bill_pnl_usdt"] + row["bill_fees_usdt"]
        row["gross_win_rate"] = row["gross_wins"] / closed if closed > 0 else None
        row["wins"] = int(row["gross_wins"])
        row["losses"] = int(row["gross_losses"])
        if closed > 0 and row["net_pnl_usdt"] < 0 and row["losses"] == 0:
            row["wins"] = 0
            row["losses"] = closed
        elif closed > 0 and row["net_pnl_usdt"] > 0 and row["wins"] == 0:
            row["wins"] = closed
            row["losses"] = 0
        wins = int(row["wins"])
        losses = int(row["losses"])
        row["win_rate"] = wins / (wins + losses) if (wins + losses) > 0 else None
        rows.append(row)
    rows.sort(key=lambda item: (-float(item.get("net_pnl_usdt") or 0.0), str(item.get("strategy_id") or "")))
    return {
        "ok": True,
        "environment": journal.environment,
        "okx_profile": profile,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "journal_path": str(journal.path),
        "inst_ids": inst_ids,
        "strategies": rows,
        "unmatched_fills": unmatched_fills,
        "unmatched_bills": unmatched_bills,
        "fills_checked": sum(len(rows) for rows in fills_by_inst.values()),
        "bills_checked": len(bills),
        "related_bills": related_bills,
    }


def _empty_performance(environment: str, profile: str) -> dict[str, Any]:
    return {
        "ok": False,
        "environment": environment,
        "okx_profile": profile,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "strategies": [],
        "errors": ["exchange_read_failed"],
    }


def _strategy_for_decision(plans: dict[str, dict[str, Any]], decision_id: str) -> str:
    plan = plans.get(decision_id) or {}
    candidate = plan.get("candidate") if isinstance(plan.get("candidate"), dict) else {}
    return str(candidate.get("strategy_id") or "")


def _order_ids(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return []
    out: list[str] = []
    for key in ("ordId", "ord_id", "order_id", "algoId"):
        raw = value.get(key)
        if isinstance(raw, list):
            out.extend(str(item) for item in raw if item)
        elif raw:
            out.append(str(raw))
    return out


def _perf_row(strategies: dict[str, dict[str, Any]], strategy_id: str) -> dict[str, Any]:
    if strategy_id not in strategies:
        strategies[strategy_id] = {
            "submitted_orders": 0,
            "exchange_fills": 0,
            "exchange_bills": 0,
            "closed_fills": 0,
            "close_events": 0,
            "wins": 0,
            "losses": 0,
            "gross_wins": 0,
            "gross_losses": 0,
            "gross_win_rate": None,
            "open_positions": 0,
            "filled_contracts": 0.0,
            "execution_fees_usdt": 0.0,
            "exchange_fees_usdt": 0.0,
            "exchange_fill_pnl_usdt": 0.0,
            "bill_pnl_usdt": 0.0,
            "bill_fees_usdt": 0.0,
        }
    return strategies[strategy_id]


def _swap_fills(profile: str, inst_id: str, limit: int) -> list[dict[str, Any]]:
    client = KitClient(KitClientConfig(default_profile=profile))
    result = _retry_kit_read(
        lambda: client.run(
            KitCommand("swap", "fills", ("--instId", inst_id), profile=profile, timeout_sec=45)
        ).require_ok(),
        attempts=3,
    )
    rows = result.data
    if isinstance(rows, dict):
        rows = [rows]
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _account_bills(profile: str, limit: int) -> list[dict[str, Any]]:
    probe = AccountProbe(KitClient(KitClientConfig(default_profile=profile)), profile=profile)
    rows = _retry_kit_read(lambda: probe.bills(limit=limit), attempts=3)
    if isinstance(rows, dict):
        rows = [rows]
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _float(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _write_status(environment: str, payload: dict[str, Any]) -> None:
    path = ENGINE_DIR / "logs" / "ownership" / environment / "reconciliation_status.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    row = dict(payload)
    row["updated_at"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(row, indent=2, sort_keys=True))


def _write_performance(environment: str, payload: dict[str, Any]) -> None:
    path = ENGINE_DIR / "logs" / "ownership" / environment / "performance_status.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    row = dict(payload)
    row["updated_at"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(row, indent=2, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
