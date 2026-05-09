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


def parse_args() -> argparse.Namespace:
    defaults = json.loads(DEFAULT_MICRO_POLICY.read_text()) if DEFAULT_MICRO_POLICY.exists() else {}
    p = argparse.ArgumentParser(description="Run C-Auto v2 micro-live validation")
    p.add_argument("--state-id", default="micro_live_competition")
    p.add_argument("--paper-state-id", default="fixed1000_conservative")
    p.add_argument("--environment", default="competition", choices=["competition"])
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
    p.add_argument("--run-on-start-entry", action="store_true", help="Allow the first clean cycle to open entries immediately")
    p.add_argument("--interval-sec", type=float, default=300.0)
    p.add_argument("--max-cycles", type=int, default=0)
    p.add_argument("--confirm-micro-live", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="Exercise the loop without sending OKX trade commands")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if not args.confirm_micro_live and not args.dry_run:
        raise SystemExit("--confirm-micro-live is required for real-money micro-live")
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
    should_rebalance = (
        bool(freshness.get("passed"))
        and daily["allow_new_entries"]
        and (run_on_start_entry or _is_rebalance_ts(now_ts, int(args.rebalance_hours)))
        and (run_on_start_entry or last_rebalance_ts != now_ts.isoformat())
    )
    if should_rebalance:
        opened, open_events = _open_micro_positions(scored, positions, now_ts, args, max_positions)
        positions.update(opened)
        ledger.extend(open_events)
    elif not freshness.get("passed"):
        ledger.append(_event(now_ts, "skip", None, None, "freshness_gate_failed:" + ",".join(freshness.get("reasons") or [])))
    elif not daily["allow_new_entries"]:
        ledger.append(_event(now_ts, "skip", None, None, daily["block_reason"]))

    positions = _mark_positions(positions, latest_features, args)
    nav = float(args.daily_budget_usdt) + sum(float(pos.get("unrealized_pnl") or 0.0) for pos in positions.values()) + daily["realized_pnl_usdt"]
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
        "profile": "live",
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
        "realized_nav": float(args.daily_budget_usdt) + daily["realized_pnl_usdt"],
        "unrealized_pnl": sum(float(pos.get("unrealized_pnl") or 0.0) for pos in positions.values()),
        "realized_pnl": daily["realized_pnl_usdt"],
        "open_risk": sum(float(pos.get("margin_usdt", 0.0)) for pos in positions.values()),
        "positions": positions,
        "freshness": freshness,
        "live_gates_enabled": True,
        "live_gate_pass_count": 1 if freshness.get("passed") else 0,
        "metrics": _live_metrics(equity_tail, float(args.daily_budget_usdt)),
        "equity": equity_tail,
        "ledger_tail": ledger_tail,
        "last_rebalance_ts": now_ts.isoformat() if should_rebalance and _has_entry_event(ledger) else last_rebalance_ts,
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
    signals = build_committee_signals(
        group,
        now_ts,
        base_capital=float(args.fixed_notional_capital),
        base_risk=0.06,
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
        max_total_budget_usdt=float(args.per_symbol_margin_usdt) * slots,
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
            "contracts": size_contracts,
            "ct_val": float(spec["ct_val"]),
            "entry_price": entry,
            "stop_price": stop_price,
            "tp1_price": target_price,
            "exchange_stop_required": True,
            "exchange_stop_attached": True,
            "exchange_tp_attached": bool(target_price is not None and order.get("take_profit_attached")),
            "order": order,
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
                "contracts": size_contracts,
                "stop_price": stop_price,
                "tp1_price": target_price,
                "exchange_stop_attached": True,
                "exchange_tp_attached": bool(target_price is not None and order.get("take_profit_attached")),
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
    profile = "live"
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
        events.append({**_event(now_ts, "exit", symbol, pos.get("side"), reason), "pnl": pnl, "close": close, "exit_price": mark})
        remaining.pop(symbol, None)
    return remaining, events


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
    cancel = _run_okx(["okx", "--profile", "live", "--json", "swap", "orders", "--instId", inst_id, "--status", "open"])
    close = _run_okx(["okx", "--profile", "live", "--json", "swap", "close", "--instId", inst_id, "--mgnMode", "isolated", "--posSide", "net", "--autoCxl"])
    return {"ok": close["ok"], "cancel_probe": cancel, "close": close}


def _mark_positions(positions: dict[str, dict[str, Any]], latest_features: pd.DataFrame, args: argparse.Namespace) -> dict[str, dict[str, Any]]:
    out = {}
    for symbol, pos in positions.items():
        p = dict(pos)
        mark = _latest_price(latest_features, symbol)
        if _valid_number(mark):
            p["mark_price"] = mark
            p["unrealized_pnl"] = _position_pnl(p, mark, args)
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
    result = _run_okx(["okx", "--profile", "live", "--json", "market", "instruments", "--instType", "SWAP", "--instId", inst_id])
    if not result["ok"]:
        raise RuntimeError(f"instrument spec failed for {inst_id}: {result['error']}")
    data = result.get("data")
    row = data[0] if isinstance(data, list) and data else data if isinstance(data, dict) else {}
    return {
        "ct_val": float(row.get("ctVal") or 1.0),
        "lot_sz": float(row.get("lotSz") or 1.0),
        "min_sz": float(row.get("minSz") or 1.0),
    }


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
    cycle_events = list(state.get("ledger_tail", []))[-10:]
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
