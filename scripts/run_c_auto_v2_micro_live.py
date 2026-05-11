#!/usr/bin/env python3
"""Micro-live runner for C-Auto v2 competition-account validation.

This is intentionally separate from the paper runner. It reuses the same signal
and committee stack, but caps real exposure to a tiny validation budget and
writes independent live state for paper/live comparison.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
ENGINE_DIR = ROOT / "engine"
sys.path.insert(0, str(ENGINE_DIR))
sys.path.insert(0, str(ROOT / "scripts"))

from arbitration.signal_committee import arbitrate_signals, build_committee_signals  # noqa: E402
from run_c_auto_v2_paper import (  # noqa: E402
    DEFAULT_POLICY,
    _build_latest_features,
    _build_portfolio_scores,
    _candidate_snapshot,
    _dedupe_ledger_events,
    _drop_freshness_skips,
    _freshness_report,
    _is_rebalance_ts,
    _json_float,
    _latest_price,
    _live_metrics,
    _predict_policy,
    _read_frame,
    _round_trip_cost_rate,
    _valid_number,
)


LIVE_DIR = ENGINE_DIR / "logs" / "c_auto_v2_micro_live"
PAPER_DIR = ENGINE_DIR / "logs" / "c_auto_v2_paper"
CONTROL_DIR = ENGINE_DIR / "control"
DEFAULT_MICRO_POLICY = ENGINE_DIR / "config" / "micro_live_policy.json"
OKX_PROFILE = "live"


def parse_args() -> argparse.Namespace:
    defaults = json.loads(DEFAULT_MICRO_POLICY.read_text()) if DEFAULT_MICRO_POLICY.exists() else {}
    p = argparse.ArgumentParser(description="Run C-Auto v2 micro-live validation")
    p.add_argument("--state-id", default="micro_live_competition")
    p.add_argument("--paper-state-id", default="fixed1000_conservative")
    p.add_argument("--environment", default="competition", choices=["competition", "personal"])
    p.add_argument("--okx-profile", default="", help="OKX CLI profile; defaults to live for competition, otherwise environment")
    p.add_argument("--policy", default=str(DEFAULT_POLICY))
    p.add_argument("--micro-policy", default=str(DEFAULT_MICRO_POLICY))
    p.add_argument("--dataset-id", default="c_auto_feature_store_rebuild_161_ohlcv_snapshot_v1")
    p.add_argument("--quality-id", default="c_auto_dataset_quality_rebuild_161_ohlcv_v1")
    p.add_argument("--deriv-run-id", default="c_auto_live_derivatives_5m")
    p.add_argument("--snapshot-run-id", default="rebuild_161_market_snapshot_20260508")
    p.add_argument("--initial-capital", type=float, default=3000.0)
    p.add_argument("--fixed-notional-capital", type=float, default=3000.0)
    p.add_argument("--max-symbols", type=int, default=80)
    p.add_argument("--refresh-max-symbols", type=int, default=0)
    p.add_argument("--refresh-ohlcv", action="store_true")
    p.add_argument("--lookback-days", type=int, default=240)
    p.add_argument("--max-train-rows", type=int, default=250_000)
    p.add_argument("--max-market-age-sec", type=float, default=2 * 3600.0)
    p.add_argument("--min-fresh-symbols", type=int, default=20)
    p.add_argument("--require-derivatives", action="store_true")
    p.add_argument("--daily-budget-usdt", type=float, default=float(defaults.get("daily_budget_usdt", 50.0)))
    p.add_argument("--per-symbol-margin-usdt", type=float, default=float(defaults.get("per_symbol_margin_usdt", 10.0)))
    p.add_argument("--first-48h-max-positions", type=int, default=int(defaults.get("first_48h_max_positions", 2)))
    p.add_argument("--steady-state-max-positions", type=int, default=int(defaults.get("steady_state_max_positions", 5)))
    p.add_argument("--daily-stop-new-entries-loss-usdt", type=float, default=float(defaults.get("daily_stop_new_entries_loss_usdt", 15.0)))
    p.add_argument("--daily-flatten-loss-usdt", type=float, default=float(defaults.get("daily_flatten_loss_usdt", 25.0)))
    p.add_argument("--default-leverage", type=float, default=float(defaults.get("default_leverage", 1.0)))
    p.add_argument("--max-leverage", type=float, default=float(defaults.get("max_leverage", 1.0)))
    p.add_argument("--max-position-nav-loss-pct", type=float, default=float(defaults.get("max_position_nav_loss_pct", 0.0015)))
    p.add_argument("--max-stop-margin-loss-pct", type=float, default=float(defaults.get("max_stop_margin_loss_pct", 0.15)))
    p.add_argument("--min-score-quantile", type=float, default=float(defaults.get("min_score_quantile", 0.90)))
    p.add_argument("--min-volume-usd", type=float, default=float(defaults.get("min_volume_usd", 100_000.0)))
    p.add_argument("--fee-bps-per-side", type=float, default=5.0)
    p.add_argument("--slippage-bps-per-side", type=float, default=2.0)
    p.add_argument("--rebalance-hours", type=int, default=int(defaults.get("rebalance_hours", 6)))
    p.add_argument("--entry-scan-minutes", type=int, default=int(defaults.get("entry_scan_minutes", 15)))
    p.add_argument("--run-on-start-entry", action="store_true", help="Allow the first clean cycle to open entries immediately")
    p.add_argument("--interval-sec", type=float, default=300.0)
    p.add_argument("--max-cycles", type=int, default=0)
    p.add_argument("--confirm-micro-live", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="Exercise the loop without sending OKX trade commands")
    return p.parse_args()


def main() -> int:
    global OKX_PROFILE
    args = parse_args()
    if not args.confirm_micro_live and not args.dry_run:
        raise SystemExit("--confirm-micro-live is required for real-money micro-live")
    OKX_PROFILE = str(args.okx_profile or ("live" if args.environment == "competition" else args.environment))
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    stop_path = CONTROL_DIR / f"c_auto_v2_micro_live_{args.state_id}_{args.environment}.stop"
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
            state = _load_state(args)
            state["runner_status"] = "error"
            state["last_error"] = str(exc)
            state["updated_at"] = datetime.now(timezone.utc).isoformat()
            _write_state(args, state)
        if args.max_cycles > 0 and cycles >= args.max_cycles:
            _write_scheduler(args, "completed", cycles)
            break
        time.sleep(max(5.0, float(args.interval_sec)))
    return 0


def _run_cycle(args: argparse.Namespace) -> dict[str, Any]:
    micro_policy = json.loads(Path(args.micro_policy).read_text()) if Path(args.micro_policy).exists() else {}
    strategy_policy = json.loads(Path(args.policy).read_text())
    dataset_dir = ENGINE_DIR / "data" / "features" / args.dataset_id
    train_features = _read_frame(dataset_dir / "features.parquet", dataset_dir / "features.pkl").sort_index()
    train_labels = _read_frame(dataset_dir / "labels.parquet", dataset_dir / "labels.pkl").sort_index()
    latest_features = _build_latest_features(args)
    predictions = _predict_policy(strategy_policy, train_features, train_labels, latest_features, args)
    scored = _build_portfolio_scores(predictions)
    now_ts = pd.Timestamp(scored.index.get_level_values("timestamp").max())
    freshness = _freshness_report(latest_features, now_ts, args)

    previous = _load_state(args)
    positions = {str(k): dict(v) for k, v in dict(previous.get("positions") or {}).items()}
    ledger: list[dict[str, Any]] = []
    positions, reconcile_events = _reconcile_exchange_positions(args, positions, latest_features, now_ts)
    ledger.extend(reconcile_events)
    positions, close_events = _close_due_positions(args, positions, latest_features, now_ts)
    ledger.extend(close_events)
    positions = _mark_positions(positions, latest_features, args)

    daily = _daily_risk(previous, ledger, args)
    if daily["realized_pnl_usdt"] <= -abs(float(args.daily_flatten_loss_usdt)):
        positions, flat_events = _flatten_positions(args, positions, "daily_flatten_loss")
        ledger.extend(flat_events)
        daily["flattened"] = True

    start_at = str(previous.get("started_at") or datetime.now(timezone.utc).isoformat())
    max_positions = _active_max_positions(start_at, args)
    last_rebalance_ts = str(previous.get("last_rebalance_ts") or "")
    last_entry_scan_ts = str(previous.get("last_entry_scan_ts") or "")
    prior_entry_events = [
        event
        for event in list(previous.get("ledger_tail", []) or [])
        if str(event.get("event") or "") == "entry"
    ]
    run_on_start_entry = (
        bool(args.run_on_start_entry)
        and not positions
        and not prior_entry_events
    )
    scheduled_rebalance = (
        bool(freshness.get("passed"))
        and daily["allow_new_entries"]
        and _is_rebalance_ts(now_ts, int(args.rebalance_hours))
        and (run_on_start_entry or last_rebalance_ts != now_ts.isoformat())
    )
    entry_scan_due = _due_after(last_entry_scan_ts, now_ts, int(args.entry_scan_minutes) * 60)
    should_scan_entries = (
        bool(freshness.get("passed"))
        and daily["allow_new_entries"]
        and len(positions) < max_positions
        and (run_on_start_entry or scheduled_rebalance or entry_scan_due)
    )
    if should_scan_entries:
        opened, open_events = _open_micro_positions(scored, positions, now_ts, args, max_positions)
        positions.update(opened)
        ledger.extend(open_events)
    elif not freshness.get("passed"):
        ledger.append(_event(now_ts, "skip", None, None, "freshness_gate_failed:" + ",".join(freshness.get("reasons") or [])))
    elif not daily["allow_new_entries"]:
        ledger.append(_event(now_ts, "skip", None, None, daily["block_reason"]))

    positions = _mark_positions(positions, latest_features, args)
    open_impact = sum(float(pos.get("unrealized_pnl") or 0.0) for pos in positions.values())
    okx_truth = _okx_micro_truth_summary(positions, list(previous.get("ledger_tail", [])) + ledger)
    realized_pnl = daily["realized_pnl_usdt"]
    realized_source = "local_ledger"
    if okx_truth.get("ok"):
        realized_pnl = float(okx_truth.get("closed_realized_pnl") or 0.0)
        realized_source = "okx_positions_history"
        daily["okx_closed_realized_pnl_since_start"] = realized_pnl
    nav = float(args.daily_budget_usdt) + open_impact + realized_pnl
    equity_tail = _upsert_equity(list(previous.get("equity", [])), {"ts": now_ts.isoformat(), "nav": nav, "open_positions": len(positions)})[-240:]
    old_ledger = list(previous.get("ledger_tail", []))
    if freshness.get("passed"):
        old_ledger = _drop_freshness_skips(old_ledger, now_ts.isoformat())
    ledger_tail = _dedupe_ledger_events(old_ledger + ledger)[-80:]
    return {
        "available": True,
        "running": True,
        "state_id": args.state_id,
        "paper_state_id": args.paper_state_id,
        "strategy_id": "c_auto_v2_fixed1000_conservative",
        "environment": args.environment,
        "mode": "real",
        "source_mode": "micro_live",
        "profile": _okx_profile(),
        "micro_policy_id": micro_policy.get("policy_id", "c_auto_v2_micro_live_competition_v1"),
        "dataset_id": args.dataset_id,
        "policy_id": strategy_policy.get("policy_id"),
        "started_at": start_at,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "timestamp": now_ts.isoformat(),
        "daily_budget_usdt": float(args.daily_budget_usdt),
        "per_symbol_margin_usdt": float(args.per_symbol_margin_usdt),
        "max_positions": max_positions,
        "daily_risk": daily,
        "cash": nav,
        "nav": nav,
        "realized_nav": float(args.daily_budget_usdt) + realized_pnl,
        "unrealized_pnl": open_impact,
        "realized_pnl": realized_pnl,
        "realized_pnl_source": realized_source,
        "okx_truth": okx_truth,
        "open_risk": sum(float(pos.get("margin_usdt", 0.0)) for pos in positions.values()),
        "positions": positions,
        "freshness": freshness,
        "live_gates_enabled": True,
        "live_gate_pass_count": 1 if freshness.get("passed") else 0,
        "metrics": _live_metrics(equity_tail, float(args.daily_budget_usdt)),
        "equity": equity_tail,
        "ledger_tail": ledger_tail,
        "cycle_events": ledger,
        "last_rebalance_ts": now_ts.isoformat() if scheduled_rebalance else last_rebalance_ts,
        "last_entry_scan_ts": now_ts.isoformat() if should_scan_entries else last_entry_scan_ts,
        "last_entry_ts": now_ts.isoformat() if _has_entry_event(ledger) else str(previous.get("last_entry_ts") or ""),
        "run_on_start_entry_used": bool(run_on_start_entry and _has_entry_event(ledger)),
        "latest_candidates": _candidate_snapshot(scored, now_ts),
        "paper_state_path": str((PAPER_DIR / f"{args.paper_state_id}_{args.environment}.json").relative_to(ROOT)),
    }


def _open_micro_positions(
    scored: pd.DataFrame,
    positions: dict[str, dict[str, Any]],
    now_ts: pd.Timestamp,
    args: argparse.Namespace,
    max_positions: int,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    slots = max(0, int(max_positions) - len(positions))
    if slots <= 0:
        return {}, [_event(now_ts, "skip", None, None, "micro_live_no_slot")]
    group = scored.xs(now_ts, level="timestamp", drop_level=False).reset_index()
    group = group[~group["symbol"].isin(positions)]
    group["volume_usd"] = pd.to_numeric(group["volume_usd"], errors="coerce").fillna(0.0)
    group = group[group["volume_usd"] >= float(args.min_volume_usd)]
    c_auto_group = group[group["eligible"].astype(bool)].copy()
    if not c_auto_group.empty:
        threshold = c_auto_group.groupby("side")["score"].transform(lambda s: s.quantile(float(args.min_score_quantile)))
        c_auto_symbols = set(c_auto_group[c_auto_group["score"] >= threshold]["symbol"].astype(str))
        group.loc[~group["symbol"].astype(str).isin(c_auto_symbols), "eligible"] = False
    signal_base_risk = 0.06
    signal_base_capital = max(
        float(args.per_symbol_margin_usdt) / signal_base_risk,
        float(args.per_symbol_margin_usdt),
    )
    signals = build_committee_signals(
        group,
        now_ts,
        base_capital=signal_base_capital,
        base_risk=signal_base_risk,
        fee_slip_rate=_round_trip_cost_rate(args),
    )
    result = arbitrate_signals(
        signals,
        positions,
        now_ts,
        initial_capital=float(args.daily_budget_usdt),
        realized_nav=float(args.daily_budget_usdt),
        max_positions=max_positions,
        max_decisions=slots,
        max_total_budget_usdt=max(0.0, float(args.daily_budget_usdt) - _used_margin(positions)),
        min_ev=0.0,
    )
    opened: dict[str, dict[str, Any]] = {}
    events: list[dict[str, Any]] = []
    row_by_symbol = {str(row["symbol"]): row for _, row in group.iterrows()}
    for decision in result.decisions[:slots]:
        signal = decision.signal
        symbol = str(signal.symbol)
        entry = float(signal.entry)
        side = str(signal.side)
        leverage = max(1.0, min(float(args.default_leverage), float(args.max_leverage)))
        margin_usdt = min(float(args.per_symbol_margin_usdt), max(0.0, float(args.daily_budget_usdt) - _used_margin(positions) - _used_margin(opened)))
        notional_usdt = margin_usdt * leverage
        if margin_usdt <= 0 or notional_usdt <= 0:
            events.append(_event(now_ts, "skip", symbol, side, "micro_live_budget_exhausted"))
            continue
        inst_id = _symbol_to_inst_id(symbol)
        try:
            spec = _instrument_spec(inst_id)
        except Exception as exc:
            events.append(_event(now_ts, "entry_rejected", symbol, side, f"instrument_spec_failed:{exc}"))
            continue
        size_contracts = _contracts_for_notional(notional_usdt, entry, spec)
        actual_notional = size_contracts * float(spec["ct_val"]) * entry
        if size_contracts <= 0 or actual_notional <= 0:
            events.append(_event(now_ts, "skip", symbol, side, "micro_live_size_below_min"))
            continue
        stop_price = float(signal.stop) if signal.stop is not None else _fallback_stop(entry, side)
        target_price = float(signal.target) if signal.target is not None else None
        order = _place_entry_with_brackets(
            inst_id=inst_id,
            side=side,
            size_contracts=size_contracts,
            leverage=leverage,
            stop_price=stop_price,
            target_price=target_price,
            args=args,
        )
        if not order["ok"]:
            events.append({**_event(now_ts, "entry_rejected", symbol, side, order["error"]), "order": order})
            continue
        truth = _post_place_truth(inst_id, order, side)
        fill_truth = truth.get("fill_summary") if isinstance(truth.get("fill_summary"), dict) else {}
        position_truth = truth.get("position") if isinstance(truth.get("position"), dict) else {}
        avg_fill_px = _json_float(fill_truth.get("avg_fill_px"))
        filled_contracts = _json_float(fill_truth.get("filled_contracts"))
        fee_usdt = _json_float(fill_truth.get("fee_usdt"))
        exchange_avg_px = _json_float(position_truth.get("avgPx"))
        exchange_contracts = abs(_json_float(position_truth.get("pos")) or 0.0)
        truth_entry = avg_fill_px or exchange_avg_px or entry
        truth_contracts = filled_contracts or exchange_contracts or size_contracts
        actual_notional = truth_contracts * float(spec["ct_val"]) * truth_entry
        row = row_by_symbol.get(symbol, {})
        position = {
            "symbol": symbol,
            "inst_id": inst_id,
            "side": side,
            "score": float(signal.confidence),
            "expected_ev": signal.forward_ev,
            "p_target": signal.p_target,
            "decision_id": decision.decision_id,
            "committee_reason": decision.reason,
            "margin_usdt": margin_usdt,
            "notional_usdt": actual_notional,
            "requested_notional_usdt": notional_usdt,
            "leverage": leverage,
            "contracts": truth_contracts,
            "requested_contracts": size_contracts,
            "ct_val": float(spec["ct_val"]),
            "entry_price": truth_entry,
            "signal_entry_price": entry,
            "stop_price": stop_price,
            "tp1_price": target_price,
            "exchange_stop_required": True,
            "exchange_stop_attached": bool(_truth_has_live_stop(truth)),
            "exchange_tp_attached": bool(target_price is not None and _truth_has_live_tp(truth)),
            "order": order,
            "order_ids": _extract_order_ids({"order": order, "truth": truth}),
            "exchange_truth": truth,
            "exchange_fill_px": avg_fill_px,
            "exchange_fee_usdt": fee_usdt,
            "exchange_avg_px": exchange_avg_px,
            "exchange_contracts": exchange_contracts or truth_contracts,
            "exchange_upl": _json_float(position_truth.get("upl")),
            "exchange_realized_pnl": _json_float(position_truth.get("realizedPnl")),
            "truth_source": "okx_private_endpoints" if not bool(getattr(args, "dry_run", False)) else "dry_run",
            "regime": row.get("btc_regime_6") if hasattr(row, "get") else signal.metadata.get("regime"),
            "signal_family": signal.metadata.get("signal_family") or signal.strategy_id,
            "source_strategy_id": signal.strategy_id,
            "entry_ts": now_ts.isoformat(),
            "exit_ts": (now_ts + pd.Timedelta(seconds=int(signal.horizon_sec))).isoformat(),
            "horizon_hours": max(1, int(signal.horizon_sec / 3600)),
        }
        opened[symbol] = position
        events.append(
            {
                **_event(now_ts, "entry", symbol, side, signal.strategy_id),
                "decision_id": decision.decision_id,
                "margin_usdt": margin_usdt,
                "notional_usdt": actual_notional,
                "leverage": leverage,
                "contracts": truth_contracts,
                "requested_contracts": size_contracts,
                "stop_price": stop_price,
                "tp1_price": target_price,
                "entry_price": truth_entry,
                "signal_entry_price": entry,
                "exchange_stop_attached": bool(_truth_has_live_stop(truth)),
                "exchange_tp_attached": bool(target_price is not None and _truth_has_live_tp(truth)),
                "exchange_fill_px": avg_fill_px,
                "exchange_fee_usdt": fee_usdt,
                "truth_source": "okx_private_endpoints" if not bool(getattr(args, "dry_run", False)) else "dry_run",
                "order_ids": _extract_order_ids({"order": order, "truth": truth}),
            }
        )
    for note in result.notes[-8:]:
        events.append(_event(now_ts, "committee_note", None, None, note))
    if not opened and not events:
        events.append(_event(now_ts, "skip", None, None, "committee_no_accepted_signals"))
    return opened, events


def _place_entry_with_brackets(
    inst_id: str,
    side: str,
    size_contracts: float,
    leverage: float,
    stop_price: float,
    target_price: float | None,
    args: argparse.Namespace,
) -> dict[str, Any]:
    if bool(getattr(args, "dry_run", False)):
        return {
            "ok": True,
            "stage": "dry_run",
            "leverage": {"ok": True, "dry_run": True},
            "take_profit_attached": target_price is not None,
            "place": {
                "ok": True,
                "dry_run": True,
                "inst_id": inst_id,
                "side": side,
                "size_contracts": size_contracts,
                "stop_price": stop_price,
                "target_price": target_price,
            },
        }
    profile = _okx_profile()
    order_side = "buy" if side == "long" else "sell"
    lev = _run_okx(["okx", "--profile", profile, "--json", "swap", "leverage", "--instId", inst_id, "--lever", _fmt(leverage), "--mgnMode", "isolated"])
    if not lev["ok"]:
        return {"ok": False, "stage": "set_leverage", "error": lev["error"], "leverage": lev}
    cmd = [
        "okx",
        "--profile",
        profile,
        "--json",
        "swap",
        "place",
        "--instId",
        inst_id,
        "--side",
        order_side,
        "--ordType",
        "market",
        "--sz",
        _fmt(size_contracts),
        "--posSide",
        "net",
        "--tdMode",
        "isolated",
    ]
    if target_price is not None and _valid_number(target_price):
        cmd.extend(
            [
                "--tpTriggerPx",
                _fmt(float(target_price)),
                "--tpOrdPx=-1",
                "--tpTriggerPxType",
                "mark",
            ]
        )
    cmd.extend(
        [
        "--slTriggerPx",
        _fmt(stop_price),
        "--slOrdPx=-1",
        "--slTriggerPxType",
        "mark",
        ]
    )
    place = _run_okx(cmd)
    if not place["ok"]:
        return {"ok": False, "stage": "place_entry_with_brackets", "error": place["error"], "leverage": lev, "place": place}
    return {"ok": True, "stage": "placed", "leverage": lev, "place": place, "take_profit_attached": target_price is not None}


def _post_place_truth(inst_id: str, order: dict[str, Any], side: str) -> dict[str, Any]:
    """Read OKX private endpoints after placement and store source-of-truth data."""
    order_ids = _extract_order_ids(order)
    ord_ids = order_ids.get("ordId", [])
    fills = _fetch_fills(inst_id)
    matching_fills = _match_fills_by_order_ids(fills, ord_ids)
    positions = _fetch_exchange_positions() or {}
    position = positions.get(inst_id, {})
    algos = _fetch_algo_orders(inst_id)
    return {
        "source": "okx_private_endpoints",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "inst_id": inst_id,
        "side": side,
        "ord_ids": ord_ids,
        "fill_summary": _summarize_fills(matching_fills),
        "fills": matching_fills[-10:],
        "position": position,
        "algo_orders": algos,
        "algo_summary": _summarize_algo_orders(algos),
    }


def _fetch_fills(inst_id: str) -> list[dict[str, Any]]:
    result = _run_okx_read(["okx", "--profile", _okx_profile(), "--json", "swap", "fills", "--instId", inst_id])
    if not result["ok"]:
        return []
    rows = result.get("data")
    if isinstance(rows, dict):
        rows = [rows]
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _fetch_algo_orders(inst_id: str) -> list[dict[str, Any]]:
    result = _run_okx_read(["okx", "--profile", _okx_profile(), "--json", "swap", "algo", "orders", "--instId", inst_id])
    if not result["ok"]:
        result = _run_okx_read(["okx", "--profile", _okx_profile(), "--json", "swap", "algo orders", "--instId", inst_id])
    if not result["ok"]:
        result = _run_okx_read(["okx", "--profile", _okx_profile(), "--json", "swap", "algo", "orders"])
    if not result["ok"]:
        return []
    rows = result.get("data")
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict) and str(row.get("instId") or "") == inst_id]


def _match_fills_by_order_ids(fills: list[dict[str, Any]], ord_ids: list[str]) -> list[dict[str, Any]]:
    id_set = {str(value) for value in ord_ids if value}
    if not id_set:
        return fills[-5:]
    return [row for row in fills if str(row.get("ordId") or "") in id_set]


def _summarize_fills(fills: list[dict[str, Any]]) -> dict[str, Any]:
    weighted_px = 0.0
    contracts = 0.0
    fee = 0.0
    fill_pnl = 0.0
    has_fee = False
    has_pnl = False
    for row in fills:
        px = _json_float(row.get("fillPx"))
        sz = abs(_json_float(row.get("fillSz")) or 0.0)
        if px is not None and sz > 0:
            weighted_px += px * sz
            contracts += sz
        row_fee = _json_float(row.get("fee"))
        if row_fee is not None:
            fee += row_fee
            has_fee = True
        row_pnl = _json_float(row.get("fillPnl"))
        if row_pnl is not None:
            fill_pnl += row_pnl
            has_pnl = True
    return {
        "avg_fill_px": (weighted_px / contracts) if contracts > 0 else None,
        "filled_contracts": contracts if contracts > 0 else None,
        "fee_usdt": fee if has_fee else None,
        "fill_pnl_usdt": fill_pnl if has_pnl else None,
        "fill_count": len(fills),
    }


def _summarize_algo_orders(algos: list[dict[str, Any]]) -> dict[str, Any]:
    live = [row for row in algos if str(row.get("state") or "").lower() == "live"]
    return {
        "live_count": len(live),
        "live_algo_ids": [str(row.get("algoId")) for row in live if row.get("algoId")],
        "has_live_stop": any(bool(row.get("slTriggerPx")) for row in live),
        "has_live_take_profit": any(bool(row.get("tpTriggerPx")) for row in live),
    }


def _truth_has_live_stop(truth: dict[str, Any]) -> bool:
    summary = truth.get("algo_summary") if isinstance(truth, dict) else {}
    return bool(isinstance(summary, dict) and summary.get("has_live_stop"))


def _truth_has_live_tp(truth: dict[str, Any]) -> bool:
    summary = truth.get("algo_summary") if isinstance(truth, dict) else {}
    return bool(isinstance(summary, dict) and summary.get("has_live_take_profit"))


def _nullable_price(value: Any) -> float | None:
    parsed = _json_float(value)
    return parsed if parsed is not None and _valid_number(parsed) else None


def _close_due_positions(args: argparse.Namespace, positions: dict[str, dict[str, Any]], latest_features: pd.DataFrame, now_ts: pd.Timestamp) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    remaining = dict(positions)
    events: list[dict[str, Any]] = []
    for symbol, pos in list(positions.items()):
        reason = ""
        exit_ts = pd.Timestamp(pos.get("exit_ts"))
        if now_ts >= exit_ts:
            reason = "horizon"
        else:
            mark = _latest_price(latest_features, symbol)
            stop = _json_float(pos.get("stop_price"))
            target = _json_float(pos.get("tp1_price"))
            side = str(pos.get("side") or "")
            if stop is not None and _valid_number(mark):
                if (side == "long" and mark <= stop) or (side == "short" and mark >= stop):
                    reason = "local_stop_shadow"
            if not reason and target is not None and _valid_number(mark):
                if (side == "long" and mark >= target) or (side == "short" and mark <= target):
                    reason = "local_take_profit_shadow"
        if not reason:
            continue
        close = _close_position(str(pos.get("inst_id") or _symbol_to_inst_id(symbol)))
        mark = _latest_price(latest_features, symbol)
        pnl = _position_pnl(pos, mark, args)
        events.append({**_event(now_ts, "exit", symbol, pos.get("side"), reason), "pnl": pnl, "close": close, "exit_price": _nullable_price(mark)})
        remaining.pop(symbol, None)
    return remaining, events


def _reconcile_exchange_positions(
    args: argparse.Namespace,
    positions: dict[str, dict[str, Any]],
    latest_features: pd.DataFrame,
    now_ts: pd.Timestamp,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    if bool(getattr(args, "dry_run", False)) or not positions:
        return positions, []
    exchange = _fetch_exchange_positions()
    if exchange is None:
        return positions, [_event(now_ts, "reconcile_skip", None, None, "exchange_positions_unavailable")]

    remaining = dict(positions)
    events: list[dict[str, Any]] = []
    for symbol, pos in list(positions.items()):
        inst_id = str(pos.get("inst_id") or _symbol_to_inst_id(symbol))
        ex_pos = exchange.get(inst_id)
        ex_contracts = abs(_json_float((ex_pos or {}).get("pos")) or 0.0)
        local_contracts = abs(_json_float(pos.get("contracts")) or 0.0)
        if ex_pos and ex_contracts > 0:
            updated = dict(pos)
            updated["exchange_contracts"] = ex_contracts
            exchange_avg_px = _json_float(ex_pos.get("avgPx"))
            updated["exchange_avg_px"] = exchange_avg_px
            updated["exchange_mark_px"] = _json_float(ex_pos.get("markPx"))
            updated["exchange_upl"] = _json_float(ex_pos.get("upl"))
            updated["exchange_realized_pnl"] = _json_float(ex_pos.get("realizedPnl"))
            updated["exchange_position_checked_at"] = datetime.now(timezone.utc).isoformat()
            if exchange_avg_px is not None:
                if "signal_entry_price" not in updated:
                    updated["signal_entry_price"] = updated.get("entry_price")
                updated["entry_price"] = exchange_avg_px
                updated["entry_price_source"] = "okx_position_avgPx"
                ct_val = _json_float(updated.get("ct_val")) or 0.0
                if ct_val > 0:
                    updated["notional_usdt"] = ex_contracts * ct_val * exchange_avg_px
            remaining[symbol] = updated
            continue

        fill_summary = _closed_fill_summary(inst_id, pos)
        mark = _latest_price(latest_features, symbol)
        pnl = fill_summary.get("pnl")
        if pnl is None:
            pnl = _position_pnl(pos, mark, args)
        exit_price = _nullable_price(fill_summary.get("exit_price"))
        if exit_price is None:
            exit_price = _nullable_price(mark)
        events.append(
            {
                **_event(now_ts, "exit", symbol, pos.get("side"), "exchange_position_flat"),
                "pnl": pnl,
                "exit_price": exit_price,
                "exchange_reconciled": True,
                "expected_contracts": local_contracts,
                "exchange_contracts": ex_contracts,
                "fills": fill_summary.get("fills", []),
            }
        )
        remaining.pop(symbol, None)
    return remaining, events


def _fetch_exchange_positions() -> dict[str, dict[str, Any]] | None:
    result = _run_okx_read(["okx", "--profile", _okx_profile(), "--json", "account", "positions", "--instType", "SWAP"])
    if not result["ok"]:
        return None
    rows = result.get("data")
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        inst_id = str(row.get("instId") or "")
        pos = abs(_json_float(row.get("pos")) or 0.0)
        if inst_id and pos > 0:
            out[inst_id] = row
    return out


def _closed_fill_summary(inst_id: str, pos: dict[str, Any]) -> dict[str, Any]:
    rows = _fetch_fills(inst_id)
    entry_ts = _parse_ts(pos.get("entry_ts"))
    close_side = "sell" if str(pos.get("side")) == "long" else "buy"
    matched: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or str(row.get("instId") or "") != inst_id:
            continue
        fill_ts = _parse_ts(row.get("fillTime") or row.get("ts"))
        if entry_ts is not None and fill_ts is not None and fill_ts < entry_ts:
            continue
        if str(row.get("side") or "").lower() != close_side:
            continue
        fill_pnl = _json_float(row.get("fillPnl"))
        if str(row.get("reduceOnly") or "").lower() not in {"true", "1"} and fill_pnl is None:
            continue
        matched.append(row)
    pnl = 0.0
    has_pnl = False
    notional = 0.0
    exit_px_weighted = 0.0
    for row in matched:
        fill_pnl = _json_float(row.get("fillPnl"))
        fee = _json_float(row.get("fee"))
        if fill_pnl is not None:
            pnl += fill_pnl
            has_pnl = True
        if fee is not None:
            pnl += fee
            has_pnl = True
        px = _json_float(row.get("fillPx"))
        sz = abs(_json_float(row.get("fillSz")) or 0.0)
        if px is not None and sz > 0:
            exit_px_weighted += px * sz
            notional += sz
    return {
        "fills": matched[-5:],
        "pnl": pnl if has_pnl else None,
        "exit_price": (exit_px_weighted / notional) if notional > 0 else None,
    }


def _okx_micro_truth_summary(positions: dict[str, dict[str, Any]], events: list[dict[str, Any]]) -> dict[str, Any]:
    inst_ids = {str(pos.get("inst_id") or _symbol_to_inst_id(symbol)) for symbol, pos in positions.items()}
    event_ts: list[pd.Timestamp] = []
    for event in events:
        symbol = event.get("symbol")
        if symbol:
            inst_ids.add(_symbol_to_inst_id(str(symbol)))
        ts = _parse_ts(event.get("ts"))
        if ts is not None:
            event_ts.append(ts)
    if not inst_ids:
        return {"ok": True, "closed_realized_pnl": 0.0, "closed_positions": 0, "inst_ids": []}
    since = min(event_ts) if event_ts else None
    result = _run_okx_read(["okx", "--profile", _okx_profile(), "--json", "account", "positions-history", "--instType", "SWAP", "--limit", "100"])
    if not result["ok"]:
        return {"ok": False, "error": result.get("error"), "inst_ids": sorted(inst_ids)}
    rows = result.get("data")
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        rows = []
    closed = []
    for row in rows:
        if not isinstance(row, dict) or str(row.get("instId") or "") not in inst_ids:
            continue
        if str(row.get("mgnMode") or "") != "isolated":
            continue
        opened = _parse_ts(row.get("cTime"))
        if since is not None and opened is not None and opened < since:
            continue
        closed.append(row)
    realized = sum(_json_float(row.get("realizedPnl")) or 0.0 for row in closed)
    return {
        "ok": True,
        "source": "okx_positions_history",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "since": since.isoformat() if since is not None else None,
        "inst_ids": sorted(inst_ids),
        "closed_positions": len(closed),
        "closed_realized_pnl": realized,
    }


def _flatten_positions(args: argparse.Namespace, positions: dict[str, dict[str, Any]], reason: str) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    now_ts = pd.Timestamp.now(tz="UTC")
    events = []
    for symbol, pos in positions.items():
        close = _close_position(str(pos.get("inst_id") or _symbol_to_inst_id(symbol)))
        events.append({**_event(now_ts, "forced_exit", symbol, pos.get("side"), reason), "close": close})
    return {}, events


def _close_position(inst_id: str) -> dict[str, Any]:
    # This helper is only reached in real mode during normal runs. Dry-run
    # states can still call it from verification paths, so keep it inert.
    if os.environ.get("C_AUTO_MICRO_LIVE_DRY_RUN", "").lower() == "true":
        return {"ok": True, "dry_run": True, "inst_id": inst_id}
    cancel = _run_okx(["okx", "--profile", _okx_profile(), "--json", "swap", "orders", "--instId", inst_id, "--status", "open"])
    close = _run_okx(["okx", "--profile", _okx_profile(), "--json", "swap", "close", "--instId", inst_id, "--mgnMode", "isolated", "--posSide", "net", "--autoCxl"])
    return {"ok": close["ok"], "cancel_probe": cancel, "close": close}


def _mark_positions(positions: dict[str, dict[str, Any]], latest_features: pd.DataFrame, args: argparse.Namespace) -> dict[str, dict[str, Any]]:
    out = {}
    for symbol, pos in positions.items():
        p = dict(pos)
        exchange_mark = _json_float(p.get("exchange_mark_px"))
        mark = exchange_mark if exchange_mark is not None else _latest_price(latest_features, symbol)
        if _valid_number(mark):
            p["mark_price"] = mark
            exchange_upl = _json_float(p.get("exchange_upl"))
            exchange_realized = _json_float(p.get("exchange_realized_pnl"))
            if exchange_upl is not None:
                p["unrealized_pnl"] = exchange_upl + (exchange_realized or 0.0)
                p["unrealized_pnl_source"] = "okx_position_upl_plus_realized"
            else:
                p["unrealized_pnl"] = _position_pnl(p, mark, args)
                p["unrealized_pnl_source"] = "local_mark_model"
            entry = float(p.get("entry_price") or 0.0)
            if entry > 0:
                raw = mark / entry - 1.0
                p["net_return"] = raw if p.get("side") == "long" else -raw
        out[symbol] = p
    return out


def _position_pnl(pos: dict[str, Any], mark: float, args: argparse.Namespace) -> float:
    entry = float(pos.get("entry_price") or 0.0)
    notional = float(pos.get("notional_usdt") or 0.0)
    side = str(pos.get("side") or "")
    if entry <= 0 or notional <= 0 or not _valid_number(mark):
        return 0.0
    raw = mark / entry - 1.0
    gross = raw if side == "long" else -raw
    return notional * (gross - _round_trip_cost_rate(args))


def _daily_risk(previous: dict[str, Any], new_events: list[dict[str, Any]], args: argparse.Namespace) -> dict[str, Any]:
    today = datetime.now(timezone.utc).date().isoformat()
    events = list(previous.get("ledger_tail", [])) + list(new_events)
    realized = 0.0
    for event in events:
        if not str(event.get("ts") or "").startswith(today):
            continue
        if event.get("event") in {"exit", "forced_exit"}:
            try:
                realized += float(event.get("pnl") or 0.0)
            except Exception:
                pass
    block = ""
    allow = True
    if realized <= -abs(float(args.daily_stop_new_entries_loss_usdt)):
        allow = False
        block = "daily_stop_new_entries_loss"
    return {
        "date": today,
        "realized_pnl_usdt": realized,
        "allow_new_entries": allow,
        "block_reason": block,
        "stop_new_entries_loss_usdt": float(args.daily_stop_new_entries_loss_usdt),
        "flatten_loss_usdt": float(args.daily_flatten_loss_usdt),
    }


def _has_entry_event(events: list[dict[str, Any]]) -> bool:
    return any(str(event.get("event") or "") == "entry" for event in events)


def _instrument_spec(inst_id: str) -> dict[str, float]:
    cache = _read_instrument_spec_cache()
    last_error = ""
    for attempt in range(3):
        result = _run_okx(["okx", "--profile", _okx_profile(), "--json", "market", "instruments", "--instType", "SWAP", "--instId", inst_id])
        if result["ok"]:
            data = result.get("data")
            row = data[0] if isinstance(data, list) and data else data if isinstance(data, dict) else {}
            spec = {
                "ct_val": float(row.get("ctVal") or 1.0),
                "lot_sz": float(row.get("lotSz") or 1.0),
                "min_sz": float(row.get("minSz") or 1.0),
            }
            cache[inst_id] = spec
            _write_instrument_spec_cache(cache)
            return spec
        last_error = str(result["error"])
        time.sleep(0.4 * (attempt + 1))
    if inst_id in cache:
        return cache[inst_id]
    raise RuntimeError(f"instrument spec failed for {inst_id}: {last_error}")


def _read_instrument_spec_cache() -> dict[str, dict[str, float]]:
    path = LIVE_DIR / "okx_swap_instrument_specs_live.json"
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        return {}
    out: dict[str, dict[str, float]] = {}
    if isinstance(data, dict):
        for inst_id, spec in data.items():
            if not isinstance(spec, dict):
                continue
            try:
                out[str(inst_id)] = {
                    "ct_val": float(spec.get("ct_val") or spec.get("ctVal") or 1.0),
                    "lot_sz": float(spec.get("lot_sz") or spec.get("lotSz") or 1.0),
                    "min_sz": float(spec.get("min_sz") or spec.get("minSz") or 1.0),
                }
            except Exception:
                continue
    return out


def _write_instrument_spec_cache(cache: dict[str, dict[str, float]]) -> None:
    path = LIVE_DIR / "okx_swap_instrument_specs_live.json"
    try:
        path.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n")
    except Exception:
        pass


def _contracts_for_notional(notional_usdt: float, price: float, spec: dict[str, float]) -> float:
    ct_val = float(spec["ct_val"])
    lot = max(float(spec["lot_sz"]), 1e-12)
    min_sz = float(spec["min_sz"])
    if price <= 0 or ct_val <= 0:
        return 0.0
    raw = float(notional_usdt) / (float(price) * ct_val)
    contracts = math.floor(raw / lot) * lot
    if contracts < min_sz:
        return 0.0
    return round(contracts, 12)


def _run_okx(cmd: list[str]) -> dict[str, Any]:
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=45)
    stdout = proc.stdout.strip()
    stderr = proc.stderr.strip()
    data = None
    if stdout:
        try:
            data = json.loads(stdout)
        except Exception:
            data = None
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "argv": cmd,
        "stdout": stdout[-2000:],
        "stderr": stderr[-2000:],
        "data": data,
        "error": None if proc.returncode == 0 else (stderr or stdout),
    }


def _run_okx_read(cmd: list[str], attempts: int = 3) -> dict[str, Any]:
    last: dict[str, Any] | None = None
    for attempt in range(max(1, attempts)):
        last = _run_okx(cmd)
        if last["ok"]:
            return last
        time.sleep(0.5 * (attempt + 1))
    return last or {"ok": False, "error": "okx_read_failed", "data": None}


def _okx_profile() -> str:
    return OKX_PROFILE


def _load_state(args: argparse.Namespace) -> dict[str, Any]:
    path = LIVE_DIR / f"{args.state_id}_{args.environment}.json"
    if not path.exists():
        return {"positions": {}, "ledger_tail": [], "equity": [], "started_at": datetime.now(timezone.utc).isoformat()}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {"positions": {}, "ledger_tail": [], "equity": [], "started_at": datetime.now(timezone.utc).isoformat()}


def _write_state(args: argparse.Namespace, state: dict[str, Any]) -> None:
    prefix = f"{args.state_id}_{args.environment}"
    (LIVE_DIR / f"{prefix}.json").write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    if state.get("equity"):
        _append_jsonl(LIVE_DIR / f"{prefix}_equity.jsonl", [state["equity"][-1]])
    cycle_events = list(state.get("cycle_events", []))
    if cycle_events:
        _append_jsonl(LIVE_DIR / f"{prefix}_ledger.jsonl", cycle_events)


def _write_scheduler(args: argparse.Namespace, status: str, cycles: int, extra: dict[str, Any] | None = None) -> None:
    payload = {
        "scheduler_status": status,
        "heartbeat_at": datetime.now(timezone.utc).isoformat(),
        "cycles": cycles,
        "interval_sec": args.interval_sec,
        "state_id": args.state_id,
        "environment": args.environment,
        "source_mode": "micro_live",
    }
    if extra:
        payload.update(extra)
    (LIVE_DIR / f"{args.state_id}_{args.environment}_scheduler.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _append_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("a") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _upsert_equity(equity: list[dict[str, Any]], point: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [row for row in equity if str(row.get("ts") or "") != str(point.get("ts") or "")]
    rows.append(point)
    return sorted(rows, key=lambda row: str(row.get("ts") or ""))


def _active_max_positions(started_at: str, args: argparse.Namespace) -> int:
    try:
        start = pd.Timestamp(started_at)
        if start.tzinfo is None:
            start = start.tz_localize("UTC")
        age_h = (pd.Timestamp.now(tz="UTC") - start).total_seconds() / 3600.0
    except Exception:
        age_h = 0.0
    return int(args.first_48h_max_positions if age_h < 48.0 else args.steady_state_max_positions)


def _used_margin(positions: dict[str, dict[str, Any]]) -> float:
    return sum(float(pos.get("margin_usdt") or 0.0) for pos in positions.values())


def _due_after(last_ts: str, now_ts: pd.Timestamp, seconds: int) -> bool:
    if seconds <= 0 or not last_ts:
        return True
    try:
        last = pd.Timestamp(last_ts)
        if last.tzinfo is None:
            last = last.tz_localize("UTC")
        now = now_ts if now_ts.tzinfo is not None else now_ts.tz_localize("UTC")
        return (now - last).total_seconds() >= float(seconds)
    except Exception:
        return True


def _parse_ts(value: Any) -> pd.Timestamp | None:
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
            return pd.Timestamp(int(value), unit="ms", tz="UTC")
        ts = pd.Timestamp(value)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return ts
    except Exception:
        return None


def _extract_order_ids(payload: Any) -> dict[str, list[str]]:
    ids: dict[str, list[str]] = {"ordId": [], "algoId": [], "clOrdId": []}

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ids and item:
                    text = str(item)
                    if text not in ids[key]:
                        ids[key].append(text)
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(payload)
    return {key: value for key, value in ids.items() if value}


def _symbol_to_inst_id(symbol: str) -> str:
    return symbol.replace("/USDT", "").replace(":USDT", "").replace("/", "-") + "-USDT-SWAP"


def _fallback_stop(entry: float, side: str) -> float:
    return entry * (0.975 if side == "long" else 1.025)


def _fmt(value: float) -> str:
    value = float(value)
    if value.is_integer():
        return str(int(value))
    return f"{value:.12f}".rstrip("0").rstrip(".")


def _event(ts: pd.Timestamp, event: str, symbol: Any, side: Any, reason: str) -> dict[str, Any]:
    return {"ts": ts.isoformat(), "event": event, "symbol": symbol, "side": side, "reason": reason, "pnl": None, "net_return": None}


if __name__ == "__main__":
    raise SystemExit(main())
