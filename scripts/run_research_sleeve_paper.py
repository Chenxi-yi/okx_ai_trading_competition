#!/usr/bin/env python3
"""Run lightweight research sleeves in paper mode with standard ledger/equity logs."""

from __future__ import annotations

import argparse
import atexit
import errno
import json
import math
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from run_trend_pullback_reversal_variants import fit_cluster_model, predict_clusters


ROOT = Path(__file__).resolve().parents[1]
ENGINE_DIR = ROOT / "engine"
CACHE_DIR = ENGINE_DIR / "data" / "cache"
FEATURE_DIR = ENGINE_DIR / "data" / "features"
US_EQUITY_CACHE_DIR = CACHE_DIR / "us_equities_yfinance_1d"
LOG_DIR = ENGINE_DIR / "logs" / "research_sleeves"
CONTROL_DIR = ENGINE_DIR / "control"
REGISTRY_PATH = ENGINE_DIR / "config" / "strategy_registry.json"
DEFAULT_FEE_BPS_PER_SIDE = 5.0
DEFAULT_SLIPPAGE_BPS_PER_SIDE = 2.0
MIN_TARGET_NET_PROFIT_BPS = 10.0
MAX_TREND_FEATURE_AGE_SEC = 2 * 3600
sys.path.insert(0, str(ENGINE_DIR))
sys.path.insert(0, str(ROOT / "scripts"))

from accounting import LiveOwnershipJournal  # noqa: E402
from arbitration.signal_committee import arbitrate_signals, candidate_trade_from_signal, candidate_trade_to_dict  # noqa: E402
from arbitration.thesis_exit import thesis_contract  # noqa: E402
from contracts import ApprovedTradePlan, Decision, ExecutionReceipt, InstrumentSpec, Signal  # noqa: E402
from execution.router import ExecutionConfig, LiveExecutionRouter  # noqa: E402
from kit import KitClient, KitClientConfig, KitExecutionGateway  # noqa: E402
from position import LivePositionLifecycleService  # noqa: E402
from run_c_auto_v2_paper import _build_latest_features as _build_c_auto_latest_features  # noqa: E402

OKX_ENV_CREDENTIAL_KEYS = {
    "OKX_API_KEY",
    "OKX_SECRET_KEY",
    "OKX_API_SECRET",
    "OKX_PASSPHRASE",
}


@dataclass
class Position:
    symbol: str
    side: str
    entry_ts: str
    entry_price: float
    notional: float
    leverage: float
    stop: float
    target: float | None
    source_strategy_id: str
    signal_family: str
    thesis_contract: dict[str, Any]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run research sleeve paper strategy")
    p.add_argument("--strategy-id", required=True)
    p.add_argument("--environment", choices=["personal", "competition"], default="personal")
    p.add_argument("--state-id", default="")
    p.add_argument("--initial-capital", type=float, default=10.0)
    p.add_argument("--symbols", default="")
    p.add_argument("--threshold", type=float, default=0.01)
    p.add_argument("--loop", action="store_true")
    p.add_argument("--fresh-start", action="store_true")
    p.add_argument("--execution", choices=["sim", "live"], default="sim")
    p.add_argument("--okx-profile", default="")
    p.add_argument("--parameter-set-id", default="")
    p.add_argument("--live-dry-run", action="store_true")
    p.add_argument("--interval-sec", type=float, default=300.0)
    p.add_argument("--max-cycles", type=int, default=0)
    p.add_argument("--post-exit-cooldown-hours", type=float, default=6.0)
    return p.parse_args()


def _resolve_and_validate_profile(args: argparse.Namespace) -> None:
    profile_by_environment = {
        "competition": "live",
        "personal": "personal",
    }
    expected = profile_by_environment.get(str(args.environment))
    if not expected:
        raise ValueError(f"unsupported environment: {args.environment}")
    supplied = str(args.okx_profile or "").strip()
    if not supplied:
        args.okx_profile = expected
    elif supplied != expected:
        raise ValueError(
            f"environment/profile mismatch: environment={args.environment} requires "
            f"--okx-profile {expected}, got {supplied}"
        )


def main() -> int:
    args = parse_args()
    _resolve_and_validate_profile(args)
    _require_environment_runner_for_live(args)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    state_id = args.state_id or args.strategy_id
    _claim_exclusive_strategy_lock(args.strategy_id, args.environment)
    stop_path = CONTROL_DIR / f"research_sleeve_{state_id}_{args.environment}.stop"
    if stop_path.exists():
        stop_path.unlink()
    cycles = 0
    while True:
        if stop_path.exists():
            _write_scheduler(args, state_id, "stopped", cycles)
            return 0
        try:
            state = _run_cycle(args, state_id)
            _append_outputs(args, state_id, state)
            cycles += 1
            args.fresh_start = False
            _write_scheduler(args, state_id, "running", cycles)
        except Exception as exc:
            cycles += 1
            _write_scheduler(args, state_id, "error", cycles, {"last_error": str(exc)})
            if not args.loop:
                raise
        if not args.loop or (args.max_cycles > 0 and cycles >= args.max_cycles):
            _write_scheduler(args, state_id, "completed", cycles)
            return 0
        time.sleep(max(5.0, float(args.interval_sec)))


def _require_environment_runner_for_live(args: argparse.Namespace) -> None:
    if str(args.execution) != "live":
        return
    if os.environ.get("OKX_ENVIRONMENT_RUNNER") == "1":
        if bool(getattr(args, "live_dry_run", False)):
            state_id = str(args.state_id or args.strategy_id)
            if args.environment == "competition" and state_id == str(args.strategy_id):
                raise SystemExit("live dry-run must use a non-production --state-id")
        return
    raise SystemExit(
        "live strategy adapters must be started by the environment runner; "
        "use the launcher environment start path instead of running this script directly"
    )


def _kill_switch_state() -> dict[str, Any]:
    path = CONTROL_DIR / "kill.switch"
    if not path.exists():
        return {"active": False}
    try:
        payload = json.loads(path.read_text())
    except Exception:
        payload = {"reason": path.read_text(errors="ignore").strip() or "kill switch present"}
    return {"active": True, **payload}


def _claim_exclusive_strategy_lock(strategy_id: str, environment: str) -> None:
    conflict = _find_conflicting_strategy_process(strategy_id, environment)
    if conflict:
        raise SystemExit(
            f"strategy {strategy_id} already running in {conflict.get('environment')} pid={conflict.get('pid')}; "
            "personal and competition cannot run the same strategy simultaneously"
        )
    safe_strategy = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in strategy_id)
    lock_path = CONTROL_DIR / f"exclusive_strategy_{safe_strategy}.lock"
    payload = {"pid": os.getpid(), "environment": environment, "strategy_id": strategy_id, "ts": datetime.now(timezone.utc).isoformat()}
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            existing = _read_lock(lock_path)
            existing_pid = int(existing.get("pid") or 0) if isinstance(existing, dict) else 0
            existing_env = str(existing.get("environment") or "") if isinstance(existing, dict) else ""
            if existing_pid and _pid_alive(existing_pid):
                if existing_env != environment:
                    raise SystemExit(
                        f"strategy {strategy_id} already running in {existing_env}; "
                        "personal and competition cannot run the same strategy simultaneously"
                    )
                raise SystemExit(f"strategy {strategy_id} already running in {environment} pid={existing_pid}")
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
            continue
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, sort_keys=True)

        def _release() -> None:
            current = _read_lock(lock_path)
            if isinstance(current, dict) and int(current.get("pid") or 0) == os.getpid():
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass

        atexit.register(_release)
        return


def _read_lock(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError as exc:
        if getattr(exc, "errno", None) == errno.EPERM:
            return True
        return False


def _find_conflicting_strategy_process(strategy_id: str, environment: str) -> dict[str, Any] | None:
    try:
        proc = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if not stripped or "scripts/run_research_sleeve_paper.py" not in stripped:
            continue
        try:
            pid_raw, command = stripped.split(None, 1)
            pid = int(pid_raw)
        except ValueError:
            continue
        if pid == os.getpid():
            continue
        proc_strategy = _command_arg(command, "--strategy-id")
        proc_env = _command_arg(command, "--environment") or "personal"
        if proc_strategy == strategy_id and proc_env in {"personal", "competition"} and proc_env != environment:
            return {"pid": pid, "environment": proc_env, "command": command}
    return None


def _command_arg(command: str, key: str) -> str | None:
    parts = command.split()
    for idx, part in enumerate(parts):
        if part == key and idx + 1 < len(parts):
            return parts[idx + 1]
        if part.startswith(f"{key}="):
            return part.split("=", 1)[1]
    return None


def _run_cycle(args: argparse.Namespace, state_id: str) -> dict[str, Any]:
    previous = {} if bool(getattr(args, "fresh_start", False)) else _load_state(state_id, args.environment)
    position_fields = set(Position.__dataclass_fields__)
    positions = {
        k: Position(**{field: value for field, value in v.items() if field in position_fields})
        for k, v in (previous.get("positions") or {}).items()
    }
    realized_nav = float(previous.get("realized_nav") or args.initial_capital)
    ledger: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc).isoformat()

    params = _parameter_params(args)
    if args.strategy_id in {"btc_weekly_swing_1p5x", "btc_weekly_swing_3x"}:
        candidates, marks = _btc_weekly_swing_signals(args)
    elif args.strategy_id == "btc_daily_breakout_swing":
        candidates, marks = _btc_daily_breakout_swing_signals(args)
    elif args.strategy_id.startswith("us_equity_token_"):
        candidates, marks = _stock_token_signals(args)
    elif args.strategy_id.startswith("trend_pullback_reversal_"):
        candidates, marks = _trend_pullback_signals(args, params)
    else:
        raise ValueError(f"unsupported strategy-id: {args.strategy_id}")
    live_position_error = ""
    if args.execution == "live":
        try:
            live_position_rows = _live_position_rows(args)
        except Exception as exc:
            live_position_rows = {}
            live_position_error = str(exc)
            candidates = []
            ledger.append(
                {
                    "ts": now,
                    "event": "live_position_check_error",
                    "strategy_id": args.strategy_id,
                    "source_strategy_id": args.strategy_id,
                    "reason": "skip_cycle_without_opening_orders",
                    "error": live_position_error,
                }
            )
    else:
        live_position_rows = {}
    live_open_symbols = set(live_position_rows)
    cooldown_hours = float(args.post_exit_cooldown_hours)
    if args.strategy_id == "us_equity_token_equity_momentum":
        cooldown_hours = max(cooldown_hours, 36.0)
    cooldown_symbols = _recent_exit_symbols(state_id, args.environment, cooldown_hours, now)
    if args.execution == "live":
        for symbol in set(positions) | {str(cand["symbol"]) for cand in candidates}:
            live_mark = _live_ticker_last(args, symbol)
            if live_mark > 0:
                marks[symbol] = live_mark
        recovered = _recover_live_positions_from_ledger(args, state_id, live_position_rows, marks, existing_symbols=set(positions))
        positions.update(recovered)
        unknown_live_symbols = sorted(symbol for symbol in live_open_symbols if symbol not in positions)
        if unknown_live_symbols:
            candidates = []
            ledger.append(
                {
                    "ts": now,
                    "event": "entry_blocked",
                    "strategy_id": args.strategy_id,
                    "source_strategy_id": args.strategy_id,
                    "reason": "unknown_exchange_positions",
                    "symbols": unknown_live_symbols,
                }
            )
        for symbol, pos in list(positions.items()):
            if symbol in live_open_symbols:
                continue
            exit_price, exit_pnl, exit_ts, exit_reason = _external_live_exit_from_bills(args, pos)
            mark = exit_price or marks.get(symbol) or pos.stop
            pnl = exit_pnl if exit_pnl is not None else _pnl(pos, mark)
            realized_nav += pnl
            ledger.append(
                _event(
                    exit_ts or now,
                    "external_exit",
                    pos,
                    mark,
                    pnl=pnl,
                    reason=exit_reason or "live_position_missing",
                )
            )
            cooldown_symbols.add(symbol)
            positions.pop(symbol, None)

    for symbol, pos in list(positions.items()):
        if pos.source_strategy_id == "btc_daily_breakout_swing":
            refreshed, thesis_exit_reason = _refresh_btc_daily_breakout_thesis(pos, args)
            positions[symbol] = refreshed
            if thesis_exit_reason:
                mark = marks.get(symbol, refreshed.entry_price)
                pnl = _net_pnl(refreshed, mark)
                live_order = _close_live_position(args, refreshed) if args.execution == "live" else None
                realized_nav += pnl
                ledger.append(_event(now, "exit", refreshed, mark, pnl=pnl, reason=thesis_exit_reason, live_order=live_order))
                positions.pop(symbol, None)

    lifecycle_exits = _research_lifecycle_exits(positions, marks, now)
    for exit_plan in lifecycle_exits:
        pos = positions.get(exit_plan.symbol)
        if pos is None:
            continue
        mark = exit_plan.mark or marks.get(exit_plan.symbol) or pos.entry_price
        pnl = _net_pnl(pos, mark)
        live_order = _close_live_position(args, pos) if args.execution == "live" else None
        realized_nav += pnl
        ledger.append(
            _event(
                now,
                "exit",
                pos,
                mark,
                pnl=pnl,
                reason=_research_exit_reason(exit_plan.reason),
                live_order=live_order,
            )
            | {"position_intent": _position_intent_payload(exit_plan.intent)}
        )
        positions.pop(exit_plan.symbol, None)

    kill_switch = _kill_switch_state()
    if kill_switch.get("active"):
        if candidates:
            ledger.append(
                {
                    "ts": now,
                    "event": "entry_blocked",
                    "strategy_id": args.strategy_id,
                    "source_strategy_id": args.strategy_id,
                    "reason": "kill_switch_active",
                    "kill_switch": kill_switch,
                }
            )
        candidates = []

    if not positions:
        if args.strategy_id.startswith("trend_pullback_reversal_"):
            candidates, committee_events = _submit_trend_candidates_to_committee(args, candidates, positions, realized_nav, now)
            ledger.extend(committee_events)
        for cand in candidates:
            symbol = str(cand["symbol"])
            if symbol in positions:
                continue
            if symbol in live_open_symbols:
                continue
            if symbol in cooldown_symbols:
                ledger.append(_candidate_skip_event(now, args.strategy_id, symbol, str(cand["signal_family"]), f"post_exit_cooldown_{cooldown_hours:g}h"))
                continue
            notional = min(float(cand["budget"]), max(0.0, realized_nav)) * float(cand["leverage"])
            if notional <= 0:
                continue
            pos = Position(
                symbol=symbol,
                side=str(cand["side"]),
                entry_ts=now,
                entry_price=float(cand["entry_price"]),
                notional=notional,
                leverage=float(cand["leverage"]),
                stop=float(cand["stop"]),
                target=cand.get("target"),
                source_strategy_id=args.strategy_id,
                signal_family=str(cand["signal_family"]),
                thesis_contract=dict(cand["thesis_contract"]),
            )
            if args.execution == "live":
                pos = _refresh_live_entry(args, pos)
            fee_guard = _fee_guard(pos)
            if pos.target is not None and not fee_guard["target_net_profitable"]:
                ledger.append(
                    _candidate_skip_event(
                        now,
                        args.strategy_id,
                        symbol,
                        str(cand["signal_family"]),
                        "target_not_net_profitable_after_fees",
                    )
                    | fee_guard
                )
                continue
            live_order = _place_live_entry(args, pos) if args.execution == "live" else None
            positions[symbol] = pos
            ledger.append(_event(now, "entry", pos, pos.entry_price, reason=str(cand["signal_family"]), live_order=live_order) | fee_guard)
            break

    unrealized = sum(_pnl(pos, marks.get(pos.symbol, pos.entry_price)) for pos in positions.values())
    return {
        "updated_at": now,
        "strategy_id": args.strategy_id,
        "state_id": state_id,
        "environment": args.environment,
        "execution": args.execution,
        "okx_profile": args.okx_profile if args.execution == "live" else None,
        "initial_capital": float(args.initial_capital),
        "realized_nav": realized_nav,
        "nav": realized_nav + unrealized,
        "unrealized_pnl": unrealized,
        "positions": {symbol: vars(pos) | {"mark_price": marks.get(symbol), "unrealized_pnl": _pnl(pos, marks.get(symbol, pos.entry_price))} for symbol, pos in positions.items()},
        "ledger_events": ledger,
        "candidate_count": len(candidates),
        "last_error": live_position_error,
    }


def _candidate_skip_event(ts: str, strategy_id: str, symbol: str, signal_family: str, reason: str) -> dict[str, Any]:
    return {
        "ts": ts,
        "event": "candidate_skip",
        "symbol": symbol,
        "strategy_id": strategy_id,
        "source_strategy_id": strategy_id,
        "signal_family": signal_family,
        "reason": reason,
    }


def _research_lifecycle_exits(
    positions: dict[str, Position],
    marks: dict[str, float],
    now: str,
) -> tuple[Any, ...]:
    raw_positions = {symbol: vars(pos) for symbol, pos in positions.items()}
    now_dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    return LivePositionLifecycleService().exit_plans(raw_positions, marks, now=now_dt)


def _research_exit_reason(reason: str) -> str:
    return {
        "target_hit": "target",
        "stop_hit": "thesis_stop",
        "time_stop": "time_stop",
    }.get(str(reason), str(reason) or "position_manager_exit")


def _position_intent_payload(intent: Any) -> dict[str, Any]:
    return {
        "decision_id": getattr(intent, "decision_id", ""),
        "strategy_id": getattr(intent, "strategy_id", ""),
        "inst_id": getattr(intent, "inst_id", ""),
        "action": getattr(intent, "action", ""),
        "reduce_only": bool(getattr(intent, "reduce_only", False)),
        "reason": getattr(intent, "reason", ""),
        "metadata": dict(getattr(intent, "metadata", {}) or {}),
    }


def _submit_trend_candidates_to_committee(
    args: argparse.Namespace,
    candidates: list[dict[str, Any]],
    positions: dict[str, Position],
    realized_nav: float,
    now: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    now_ts = pd.Timestamp(now)
    signals: list[Signal] = []
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for cand in candidates:
        signal = _trend_candidate_signal(args, cand, now_ts)
        signals.append(signal)
        by_key[(signal.symbol, signal.side)] = cand
    candidate_contracts = [candidate_trade_to_dict(candidate_trade_from_signal(signal)) for signal in signals]
    max_positions = int(_parameter_params(args).get("max_concurrent_positions") or 3)
    budget_total = max(0.0, float(args.initial_capital) - sum(float(pos.notional) / max(float(pos.leverage), 1.0) for pos in positions.values()))
    result = arbitrate_signals(
        signals,
        {symbol: vars(pos) for symbol, pos in positions.items()},
        now_ts,
        initial_capital=float(args.initial_capital),
        realized_nav=float(realized_nav),
        max_positions=max_positions,
        max_decisions=max_positions,
        max_total_budget_usdt=budget_total,
        min_ev=0.0,
        round_trip_cost_rate=_round_trip_cost_rate(),
    )
    accepted: list[dict[str, Any]] = []
    for decision in result.decisions:
        cand = dict(by_key.get((decision.signal.symbol, decision.signal.side)) or {})
        if not cand:
            continue
        cand["committee_decision_id"] = decision.decision_id
        cand["committee_reason"] = decision.reason
        cand["committee_size_usdt"] = decision.size_usdt
        accepted.append(cand)
    events = [
        {
            "ts": now,
            "event": "committee_submission",
            "strategy_id": args.strategy_id,
            "source_strategy_id": args.strategy_id,
            "candidate_count": len(candidates),
            "signal_count": len(signals),
            "candidate_contract_count": len(candidate_contracts),
            "candidate_contracts": candidate_contracts[:25],
            "accepted_count": len(accepted),
            "rejected_count": len(result.rejected),
            "notes": list(result.notes),
            "accepted": [
                {
                    "symbol": decision.signal.symbol,
                    "side": decision.signal.side,
                    "decision_id": decision.decision_id,
                    "size_usdt": decision.size_usdt,
                    "reason": decision.reason,
                    "forward_ev": decision.signal.forward_ev,
                }
                for decision in result.decisions
            ],
            "rejected": [
                {
                    "symbol": signal.symbol,
                    "side": signal.side,
                    "strategy_id": signal.strategy_id,
                    "forward_ev": signal.forward_ev,
                }
                for signal in result.rejected
            ],
        }
    ]
    return accepted, events


def _trend_candidate_signal(args: argparse.Namespace, cand: dict[str, Any], now_ts: pd.Timestamp) -> Signal:
    entry = float(cand["entry_price"])
    side = str(cand["side"])
    target = cand.get("target")
    stop = cand.get("stop")
    target_pct = abs(float(target) / entry - 1.0) if target is not None and entry > 0 else 0.03
    stop_pct = abs(float(stop) / entry - 1.0) if stop is not None and entry > 0 else 0.015
    quality = float((cand.get("thesis_contract") or {}).get("quality_score") or 0.0)
    p_target = max(0.51, min(0.66, 0.53 + quality * 0.12 - _round_trip_cost_rate()))
    confidence = max(0.52, min(0.82, 0.52 + quality * 0.25))
    metadata = dict(cand.get("thesis_contract") or {})
    metadata.update(
        {
            "signal_family": cand.get("signal_family") or args.strategy_id,
            "score": quality,
            "risk_budget_usdt": float(cand.get("budget") or args.initial_capital),
            "target_pct": target_pct,
            "stop_pct": stop_pct,
            "thesis_contract": thesis_contract(
                strategy_id=args.strategy_id,
                side=side,
                signal_family=str(cand.get("signal_family") or args.strategy_id),
                regime=str(metadata.get("regime") or ""),
                score=quality,
            ),
        }
    )
    return Signal(
        strategy_id=args.strategy_id,
        symbol=str(cand["symbol"]),
        side=side,  # type: ignore[arg-type]
        timestamp=now_ts.to_pydatetime(),
        entry=entry,
        target=float(target) if target is not None else None,
        stop=float(stop) if stop is not None else None,
        horizon_sec=int(float((cand.get("thesis_contract") or {}).get("max_hold_hours") or 12) * 3600),
        p_target=p_target,
        adverse_pct_estimate=stop_pct,
        confidence=confidence,
        metadata=metadata,
    )


def _parameter_params(args: argparse.Namespace) -> dict[str, Any]:
    parameter_set_id = str(getattr(args, "parameter_set_id", "") or "")
    if not parameter_set_id or not REGISTRY_PATH.exists():
        return {}
    try:
        data = json.loads(REGISTRY_PATH.read_text())
    except Exception:
        return {}
    for item in data.get("parameter_sets", []):
        if str(item.get("parameter_set_id") or "") == parameter_set_id:
            return dict(item.get("params") or {})
    return {}


def _trend_pullback_signals(args: argparse.Namespace, params: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, float]]:
    dataset_id = str(params.get("dataset_id") or "c_auto_feature_store_rebuild_161_ohlcv_snapshot_v1")
    path = FEATURE_DIR / dataset_id / "features.parquet"
    if not path.exists():
        return [], {}
    historical = pd.read_parquet(path).sort_index()
    if historical.empty:
        return [], {}
    latest = _trend_live_latest_features(args, params)
    df = pd.concat([historical, latest]).sort_index()
    required = {
        "close",
        "volume_usd",
        "listing_age_days",
        "train_eligible_90d",
        "ret_1",
        "ret_3",
        "range_pct",
        "close_to_high",
        "close_to_low",
        "atr_14_pct",
        "rv_24",
        "vol_z_24",
        "trend_eff_24",
        "funding_z_24",
        "oi_z_24",
        "ls_z_24",
        "h4_ret_1",
        "h4_ret_6",
        "btc_ret_4h",
        "btc_ret_24h",
        "btc_rv_24h",
        "btc_drawdown_30d",
    }
    for col in required:
        if col not in df.columns:
            df[col] = 0.0
    for col in required:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    candidates = _trend_pullback_candidate_frame(latest, params, require_forward=False)
    marks = _latest_marks(df)
    if candidates.empty:
        return [], marks
    selected = _select_trend_pullback_variant(args.strategy_id, candidates, df, params)
    if selected.empty:
        return [], marks
    selected = selected.sort_values("quality_score", ascending=False)
    rows: list[dict[str, Any]] = []
    budget = float(params.get("order_margin_usdt") or params.get("runtime_budget_usdt") or args.initial_capital)
    leverage = float(params.get("leverage") or 1.0)
    target_pct = float(params.get("target_pct") or 0.05)
    stop_pct = float(params.get("stop_pct") or 0.015)
    for row in selected.to_dict(orient="records"):
        symbol = str(row["symbol"])
        entry = float(row["entry"])
        side = str(row["side"])
        stop = entry * (1.0 - stop_pct) if side == "long" else entry * (1.0 + stop_pct)
        target = entry * (1.0 + target_pct) if side == "long" else entry * (1.0 - target_pct)
        rows.append(
            {
                "symbol": _registry_symbol(symbol),
                "side": side,
                "entry_price": entry,
                "budget": budget,
                "leverage": leverage,
                "stop": stop,
                "target": target,
                "signal_family": args.strategy_id,
                "thesis_contract": {
                    "contract_id": "trend_pullback_reversal_runtime_v1",
                    "entry_reason": str(params.get("entry_filter") or args.strategy_id),
                    "quality_score": float(row.get("quality_score") or 0.0),
                    "source_strategy_id": args.strategy_id,
                    "runtime_budget_usdt": float(params.get("runtime_budget_usdt") or budget),
                    "order_margin_usdt": budget,
                    "target_pct": target_pct,
                    "stop_pct": stop_pct,
                },
            }
        )
    return rows, {_registry_symbol(k): v for k, v in marks.items()}


def _trend_live_latest_features(args: argparse.Namespace, params: dict[str, Any]) -> pd.DataFrame:
    live_args = argparse.Namespace(
        quality_id=str(params.get("quality_id") or "c_auto_dataset_quality_rebuild_161_ohlcv_v1"),
        max_symbols=int(params.get("max_symbols") or 80),
        refresh_max_symbols=0,
        refresh_ohlcv=False,
        deriv_run_id=str(params.get("deriv_run_id") or "c_auto_live_derivatives_5m"),
        snapshot_run_id=str(params.get("snapshot_run_id") or "rebuild_161_market_snapshot_20260508"),
        lookback_days=int(params.get("lookback_days") or 3),
    )
    latest = _build_c_auto_latest_features(live_args).copy()
    latest_ts = pd.Timestamp(latest.index.get_level_values("timestamp").max())
    age_sec = (pd.Timestamp.now(tz="UTC") - latest_ts).total_seconds()
    if age_sec > MAX_TREND_FEATURE_AGE_SEC:
        raise RuntimeError(
            f"trend feature cache stale: latest_ts={latest_ts.isoformat()} age_sec={age_sec:.0f} "
            f"max_age_sec={MAX_TREND_FEATURE_AGE_SEC}"
        )
    return latest


def _trend_pullback_candidate_frame(df: pd.DataFrame, params: dict[str, Any], require_forward: bool) -> pd.DataFrame:
    frame = df.copy()
    ts_index = frame.index.get_level_values("timestamp")
    latest_ts = ts_index.max()
    if not require_forward:
        frame = frame[ts_index == latest_ts].copy()
    frame = frame[
        (frame["volume_usd"].fillna(0.0) >= float(params.get("min_volume_usd") or 200_000.0))
        & (frame["listing_age_days"].fillna(0.0) >= float(params.get("min_listing_days") or 60.0))
        & (frame["train_eligible_90d"].fillna(0.0) > 0)
        & frame["close"].notna()
        & frame["h4_ret_6"].notna()
        & frame["h4_ret_1"].notna()
    ].copy()
    if frame.empty:
        return pd.DataFrame()
    max_symbols = int(params.get("max_symbols") or 80)
    latest_volume = df.groupby(level="symbol")["volume_usd"].last().sort_values(ascending=False)
    keep = set(latest_volume.head(max_symbols).index.astype(str))
    frame = frame[frame.index.get_level_values("symbol").astype(str).isin(keep)].copy()
    h4_allow = abs(float(params.get("h4_countertrend_allow") or 0.005))
    trend_min = abs(float(params.get("h4_trend_min") or 0.0))
    frame["side"] = ""
    frame.loc[(frame["h4_ret_6"] > trend_min) & (frame["h4_ret_1"] > -h4_allow), "side"] = "long"
    frame.loc[(frame["h4_ret_6"] < -trend_min) & (frame["h4_ret_1"] < h4_allow), "side"] = "short"
    grouped = df.groupby(level="symbol")
    median_abs = grouped["ret_1"].transform(lambda s: s.abs().rolling(24, min_periods=8).median())
    median_abs = median_abs.reindex(frame.index)
    counter_limit = pd.Series(
        [
            min(
                float(params.get("max_countertrend_move_pct") or 0.045),
                max(0.008, float(params.get("max_countertrend_multiple") or 4.0) * (float(x) if pd.notna(x) else 0.0)),
            )
            for x in median_abs
        ],
        index=frame.index,
    )
    long_pullback = (frame["side"] == "long") & (frame["ret_3"] < 0) & (frame["ret_3"].abs() <= counter_limit)
    short_pullback = (frame["side"] == "short") & (frame["ret_3"] > 0) & (frame["ret_3"].abs() <= counter_limit)
    near = abs(float(params.get("near_extreme_pct") or 0.003))
    loose = abs(float(params.get("loose_extreme_pct") or 0.006))
    trigger_frac = abs(float(params.get("trigger_range_frac") or 0.25))
    long_trigger = (frame["ret_1"] > 0) & (
        (frame["close_to_high"] >= -near)
        | ((frame["ret_1"] > frame["range_pct"].abs() * trigger_frac) & (frame["close_to_high"] >= -loose))
    )
    short_trigger = (frame["ret_1"] < 0) & (
        (frame["close_to_low"] <= near)
        | ((frame["ret_1"].abs() > frame["range_pct"].abs() * trigger_frac) & (frame["close_to_low"] <= loose))
    )
    events = frame[(long_pullback & long_trigger) | (short_pullback & short_trigger)].copy()
    if events.empty:
        return pd.DataFrame()
    events["counter_move"] = events["ret_3"].abs()
    events["counter_limit"] = counter_limit.loc[events.index].astype(float)
    events["counter_ratio"] = (events["counter_move"] / events["counter_limit"].replace(0, pd.NA)).clip(0, 3)
    events["h4_trend_abs"] = events["h4_ret_6"].abs()
    events["h4_trend_align"] = events["h4_ret_6"].apply(lambda x: 1.0 if x >= 0 else -1.0) * events["h4_ret_1"]
    events["reversal_ret_abs"] = events["ret_1"].abs()
    events["reversal_range_frac"] = (events["ret_1"].abs() / events["range_pct"].abs().replace(0, pd.NA)).clip(0, 5)
    events["close_location_score"] = events.apply(
        lambda row: max(0.0, min(1.0, 1.0 + float(row["close_to_high"])))
        if row["side"] == "long"
        else max(0.0, min(1.0, 1.0 - float(row["close_to_low"]))),
        axis=1,
    )
    events["quality_score"] = _trend_quality_score(events)
    if require_forward:
        horizon = int(params.get("max_hold_hours") or 12)
        cost = 2.0 * (float(params.get("fee_bps_per_side") or 5.0) + float(params.get("slippage_bps_per_side") or 2.0)) / 10000.0
        future_close = df.groupby(level="symbol")["close"].shift(-horizon).reindex(events.index)
        events["fwd_ret"] = future_close / events["close"] - 1.0
        events["gross_return"] = np.where(events["side"] == "short", -events["fwd_ret"], events["fwd_ret"])
        events["net_return"] = events["gross_return"] - cost
    out = events.reset_index()
    out["entry_ts"] = pd.to_datetime(out["timestamp"], utc=True)
    out["entry"] = out["close"].astype(float)
    required = ["entry_ts", "symbol", "side", "entry", "quality_score"]
    if require_forward:
        required.append("net_return")
    return out.replace([float("inf"), float("-inf")], pd.NA).dropna(subset=required)


def _trend_quality_score(events: pd.DataFrame) -> pd.Series:
    trend = (events["h4_trend_abs"].abs() / 0.08).clip(0, 1).fillna(0.0)
    align = (events["h4_trend_align"] / 0.02).clip(0, 1).fillna(0.0)
    pullback = (1.0 - (events["counter_ratio"] - 0.55).abs() / 0.55).clip(0, 1).fillna(0.0)
    reversal = (events["reversal_range_frac"].clip(0, 3) / 1.5).clip(0, 1).fillna(0.0)
    location = events["close_location_score"].clip(0, 1).fillna(0.0)
    vol_ok = (1.0 - (events["atr_14_pct"].fillna(0.015) - 0.015).abs() / 0.035).clip(0, 1)
    return 0.24 * trend + 0.16 * align + 0.20 * pullback + 0.18 * reversal + 0.12 * location + 0.10 * vol_ok


def _select_trend_pullback_variant(strategy_id: str, candidates: pd.DataFrame, df: pd.DataFrame, params: dict[str, Any]) -> pd.DataFrame:
    if candidates.empty:
        return candidates
    ranked = candidates.sort_values("quality_score", ascending=False).copy()
    if strategy_id.endswith("quality_top20_v1"):
        frac = float(params.get("quality_top_frac") or 0.2)
        count = max(1, math.ceil(len(ranked) * frac))
        return ranked.head(count)
    if strategy_id.endswith("rank_top1_v1"):
        return ranked.head(int(params.get("rank_top_n") or 1))
    if strategy_id.endswith("cluster_elite_quality60_v1"):
        floor = float(params.get("quality_min_score") or 0.6)
        current_ts = pd.to_datetime(ranked["entry_ts"], utc=True).max()
        historical = _trend_pullback_candidate_frame(df, params, require_forward=True)
        if historical.empty:
            return ranked.iloc[0:0].copy()
        historical["entry_ts"] = pd.to_datetime(historical["entry_ts"], utc=True)
        train_days = int(params.get("cluster_train_days") or 180)
        train = historical[
            (historical["entry_ts"] < current_ts)
            & (historical["entry_ts"] >= current_ts - pd.Timedelta(days=train_days))
        ].copy()
        cluster_min_count = int(params.get("cluster_min_count") or 80)
        cluster_args = argparse.Namespace(
            cluster_k=int(params.get("cluster_k") or 6),
            cluster_min_count=cluster_min_count,
            cluster_min_return=float(params.get("cluster_min_mean_return") or 0.0),
            cluster_min_mean_return=float(params.get("cluster_min_mean_return") or 0.0),
            cluster_min_train=int(params.get("cluster_min_train") or max(400, cluster_min_count * 3)),
            cluster_min_win_rate=float(params.get("cluster_min_win_rate") or 0.60),
        )
        if len(train) < int(cluster_args.cluster_min_train):
            return ranked.iloc[0:0].copy()
        model = fit_cluster_model(train, current_ts, cluster_args)
        if not model.eligible_clusters:
            return ranked.iloc[0:0].copy()
        current = ranked.copy()
        current["cluster"] = predict_clusters(current, model)
        selected = current[(current["quality_score"] >= floor) & current["cluster"].isin(model.eligible_clusters)]
        return selected.head(int(params.get("rank_top_n") or params.get("max_concurrent_positions") or 3))
    return ranked


def _latest_marks(df: pd.DataFrame) -> dict[str, float]:
    out: dict[str, float] = {}
    for symbol, group in df.groupby(level="symbol"):
        value = _float_or_none(group["close"].iloc[-1])
        if value is not None:
            out[str(symbol)] = float(value)
    return out


def _registry_symbol(symbol: str) -> str:
    return str(symbol).replace("/", "_").replace(":", "_")


def _recent_exit_symbols(state_id: str, environment: str, cooldown_hours: float, now: str) -> set[str]:
    if cooldown_hours <= 0:
        return set()
    path = LOG_DIR / f"{state_id}_{environment}_ledger.jsonl"
    if not path.exists():
        return set()
    try:
        now_ts = pd.Timestamp(now)
    except Exception:
        now_ts = pd.Timestamp.now(tz="UTC")
    window = pd.Timedelta(hours=float(cooldown_hours))
    out: set[str] = set()
    for line in path.read_text().splitlines()[-500:]:
        try:
            event = json.loads(line)
        except Exception:
            continue
        name = str(event.get("event") or "").lower()
        if "exit" not in name and name not in {"manual_close", "flatten"}:
            continue
        symbol = str(event.get("symbol") or "")
        if not symbol:
            continue
        try:
            ts = pd.Timestamp(str(event.get("ts") or ""))
        except Exception:
            continue
        if now_ts - ts <= window:
            out.add(symbol)
    return out


def _btc_weekly_swing_signals(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, float]]:
    df = _load_ohlcv("BTC_USDT", "1d")
    weekly = df.resample("W-SUN").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    if len(weekly) < 40:
        return [], {}
    close = weekly["close"]
    prior_high = weekly["high"].rolling(13, min_periods=8).max().shift(1)
    sma20 = close.rolling(20, min_periods=10).mean()
    last = weekly.iloc[-1]
    ts = weekly.index[-1]
    mark = float(last["close"])
    marks = {"BTC_USDT": mark}
    long_signal = mark > float(prior_high.iloc[-1]) * 1.004 and mark >= float(sma20.iloc[-1]) * 0.994
    if not long_signal:
        return [], marks
    high_water_stop = float(weekly["high"].tail(8).max()) * (1.0 - 0.24)
    sma_stop = float(sma20.iloc[-1]) * 0.994
    stop = max(high_water_stop, sma_stop)
    return [
        {
            "symbol": "BTC_USDT",
            "side": "long",
            "entry_price": mark,
            "budget": min(100.0, float(args.initial_capital)),
            "leverage": 3.0,
            "stop": stop,
            "target": None,
            "signal_family": "weekly_13w_breakout_20w_sma_trailing",
            "thesis_contract": {
                "entry_reason": f"weekly close {mark:.2f} broke prior 13w high as of {ts.date()}",
                "monitor": "weekly close remains above 20w SMA buffer and 24pct trailing stop",
                "hard_invalidation": "mark <= stop",
                "risk_budget_usdt": min(100.0, float(args.initial_capital)),
                "max_effective_leverage": 3.0,
            },
        }
    ], marks


def _btc_daily_breakout_swing_signals(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, float]]:
    df = _btc_daily_breakout_frame()
    if df.empty or len(df) < 120:
        return [], {}
    last = df.iloc[-1]
    mark = float(last["close"])
    marks = {"BTC_USDT": mark}
    long_signal = bool(last["regime_bull"]) and bool(last["breakout_signal"])
    if not long_signal:
        return [], marks
    entry = mark
    atr_stop = entry - float(last["atr"]) * 3.0
    trail_stop = entry * (1.0 - 0.18)
    stop = max(atr_stop, trail_stop)
    risk = max(1e-9, entry - stop)
    target = entry + risk * 2.0
    budget = min(100.0, float(args.initial_capital))
    return [
        {
            "symbol": "BTC_USDT",
            "side": "long",
            "entry_price": entry,
            "budget": budget,
            "leverage": 2.0,
            "stop": stop,
            "target": target,
            "signal_family": "btc_daily_80d_breakout_weekly_regime",
            "thesis_contract": {
                "contract_id": "btc_daily_breakout_swing_v1",
                "entry_reason": (
                    f"daily close {entry:.2f} broke prior 80d high "
                    f"{float(last['prior_high']):.2f}; weekly regime is bullish"
                ),
                "monitor": [
                    "weekly close remains above 20w SMA and weekly SMA slope is not materially negative",
                    "daily close remains above 100d SMA thesis line",
                    "18% high-water trailing stop is respected",
                    "2R take-profit is attached for explicit TP/SL compliance",
                ],
                "hard_invalidation": "daily thesis exit or mark <= trailing/ATR stop",
                "risk_budget_usdt": budget,
                "max_effective_leverage": 2.0,
                "lookback_days": 80,
                "weekly_sma": 20,
                "exit_sma": 100,
                "trail_stop_pct": 0.18,
                "atr_stop_mult": 3.0,
                "target_r": 2.0,
                "signal_ts": pd.Timestamp(df.index[-1]).isoformat(),
            },
        }
    ], marks


def _btc_daily_breakout_frame() -> pd.DataFrame:
    df = _load_ohlcv("BTC_USDT", "1d")
    if df.empty:
        return df
    now = pd.Timestamp.now(tz="UTC")
    # Avoid trading on an unfinished daily candle.
    if pd.Timestamp(df.index[-1]).date() >= now.date():
        df = df.iloc[:-1]
    if df.empty:
        return df
    weekly = df.resample("W-SUN").agg({"close": "last"}).dropna()
    weekly["weekly_sma"] = weekly["close"].rolling(20, min_periods=10).mean()
    weekly["weekly_slope"] = weekly["weekly_sma"] / weekly["weekly_sma"].shift(1) - 1.0
    df = df.join(
        weekly[["close", "weekly_sma", "weekly_slope"]]
        .rename(columns={"close": "weekly_close"})
        .reindex(df.index, method="ffill")
    )
    df["exit_sma"] = df["close"].astype(float).rolling(100, min_periods=50).mean()
    prev_close = df["close"].astype(float).shift(1)
    tr = pd.concat(
        [
            df["high"].astype(float) - df["low"].astype(float),
            (df["high"].astype(float) - prev_close).abs(),
            (df["low"].astype(float) - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    df["atr"] = tr.rolling(14, min_periods=7).mean()
    df["prior_high"] = df["high"].astype(float).rolling(80, min_periods=40).max().shift(1)
    df["regime_bull"] = (df["weekly_close"].astype(float) > df["weekly_sma"].astype(float)) & (df["weekly_slope"].astype(float) >= -0.005)
    df["breakout_signal"] = df["regime_bull"] & (df["close"].astype(float) > df["prior_high"].astype(float))
    return df.dropna(subset=["weekly_sma", "weekly_slope", "exit_sma", "atr", "prior_high"])


def _refresh_btc_daily_breakout_thesis(pos: Position, args: argparse.Namespace) -> tuple[Position, str]:
    df = _btc_daily_breakout_frame()
    if df.empty:
        return pos, ""
    last = df.iloc[-1]
    mark = _live_ticker_last(args, pos.symbol) if args.execution == "live" else float(last["close"])
    if mark <= 0 or not math.isfinite(mark):
        mark = float(last["close"])
    entry_ts = pd.Timestamp(pos.entry_ts)
    since_entry = df.loc[df.index >= entry_ts] if not df.empty else df
    high_water = float(since_entry["high"].astype(float).max()) if not since_entry.empty else max(pos.entry_price, mark)
    trail_stop = high_water * (1.0 - 0.18)
    updated_stop = max(float(pos.stop), trail_stop)
    contract = dict(pos.thesis_contract)
    contract["last_thesis_check"] = pd.Timestamp(df.index[-1]).isoformat()
    contract["last_close"] = float(last["close"])
    contract["last_exit_sma"] = float(last["exit_sma"])
    contract["last_weekly_slope"] = float(last["weekly_slope"])
    contract["last_weekly_close"] = float(last["weekly_close"])
    contract["last_weekly_sma"] = float(last["weekly_sma"])
    contract["high_water"] = high_water
    refreshed = Position(
        symbol=pos.symbol,
        side=pos.side,
        entry_ts=pos.entry_ts,
        entry_price=pos.entry_price,
        notional=pos.notional,
        leverage=pos.leverage,
        stop=updated_stop,
        target=pos.target,
        source_strategy_id=pos.source_strategy_id,
        signal_family=pos.signal_family,
        thesis_contract=contract,
    )
    if not bool(last["regime_bull"]):
        return refreshed, "weekly_regime_lost"
    if float(last["close"]) < float(last["exit_sma"]):
        return refreshed, "daily_100sma_thesis_lost"
    if mark <= updated_stop:
        return refreshed, "thesis_trailing_stop"
    return refreshed, ""


def _stock_token_signals(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, float]]:
    default_symbols = {
        "us_equity_token_dislocation_reversion": "AMZN,TSLA,NVDA",
        "us_equity_token_okx_momentum": "COIN,HOOD,AMZN,GOOGL",
        "us_equity_token_equity_momentum": "AMZN,GOOGL,NVDA",
    }.get(str(args.strategy_id), "NVDA,TSLA,GOOGL,AMZN,MSTR")
    tickers = [s.strip().upper() for s in (args.symbols or default_symbols).split(",") if s.strip()]
    threshold = float(getattr(args, "threshold", 0.01) or 0.01)
    if args.strategy_id in {"us_equity_token_okx_momentum", "us_equity_token_equity_momentum"}:
        threshold = max(threshold, 0.02)
    candidates: list[dict[str, Any]] = []
    marks: dict[str, float] = {}
    for ticker in tickers:
        symbol = f"{ticker}_USDT"
        okx = _load_ohlcv(symbol, "1d")
        if len(okx) < 4:
            continue
        close = okx["close"].astype(float)
        mark = float(close.iloc[-1])
        marks[symbol] = mark
        ret1 = float(close.iloc[-1] / close.iloc[-2] - 1.0)
        equity_ret1 = _equity_ret1(ticker)
        side = ""
        signal_family = ""
        signal_strength = abs(ret1)
        dislocation: float | None = None
        if args.strategy_id == "us_equity_token_okx_momentum" and threshold <= abs(ret1) <= 0.06:
            if equity_ret1 is not None and (
                (ret1 > 0 and equity_ret1 >= 0.01) or (ret1 < 0 and equity_ret1 <= -0.01)
            ):
                side = "long" if ret1 > 0 else "short"
                signal_family = "okx_prevday_momentum_capped_equity_confirmed"
        elif args.strategy_id == "us_equity_token_dislocation_reversion":
            if equity_ret1 is not None:
                dislocation = ret1 - equity_ret1
                signal_strength = abs(dislocation)
                if dislocation >= threshold:
                    side = "short"
                elif dislocation <= -threshold:
                    side = "long"
                signal_family = "equity_okx_dislocation_reversion" if side else ""
            else:
                ret3 = float(close.iloc[-1] / close.iloc[-4] - 1.0)
                signal_strength = abs(ret3)
                if ret3 >= 0.05:
                    side = "short"
                elif ret3 <= -0.05:
                    side = "long"
                signal_family = "okx_dislocation_proxy_reversion" if side else ""
        elif args.strategy_id == "us_equity_token_equity_momentum":
            signal_ret = equity_ret1
            if signal_ret is not None and abs(signal_ret) >= threshold:
                side = "long" if signal_ret > 0 else "short"
                signal_family = "quality_equity_prevday_momentum"
        if not side:
            continue
        lev = 1.0
        stop_pct, target_pct = _stock_token_risk_params(str(args.strategy_id), ticker)
        stop = mark * (1.0 - stop_pct) if side == "long" else mark * (1.0 + stop_pct)
        target = mark * (1.0 + target_pct) if side == "long" else mark * (1.0 - target_pct)
        candidates.append(
            {
                "symbol": symbol,
                "side": side,
                "entry_price": mark,
                "budget": min(10.0, float(args.initial_capital)),
                "leverage": lev,
                "stop": stop,
                "target": target,
                "signal_strength": signal_strength,
                "signal_family": signal_family,
                "thesis_contract": {
                    "entry_reason": f"{signal_family} on {symbol}: okx_ret1={ret1:.4f}, equity_ret1={equity_ret1}, dislocation={dislocation}",
                    "equity_ret1": equity_ret1,
                    "okx_ret1": ret1,
                    "dislocation": dislocation,
                    "monitor": "target/stop and signal family remains active",
                    "hard_invalidation": "mark reaches stop",
                    "risk_budget_usdt": min(10.0, float(args.initial_capital)),
                    "max_effective_leverage": lev,
                    "trade_universe": tickers,
                    "entry_threshold_abs_ret": threshold,
                    "overheat_filter_abs_okx_ret": 0.06 if args.strategy_id == "us_equity_token_okx_momentum" else None,
                },
            }
        )
    candidates.sort(key=lambda row: float(row.get("signal_strength") or 0.0), reverse=True)
    return candidates, marks


def _stock_token_risk_params(strategy_id: str, ticker: str) -> tuple[float, float]:
    ticker = ticker.upper()
    if strategy_id == "us_equity_token_dislocation_reversion":
        return {
            "AMZN": (0.025, 0.035),
            "NVDA": (0.025, 0.035),
            "TSLA": (0.035, 0.050),
        }.get(ticker, (0.035, 0.050))
    if strategy_id == "us_equity_token_okx_momentum":
        return {
            "COIN": (0.030, 0.050),
            "HOOD": (0.030, 0.050),
            "AMZN": (0.030, 0.050),
            "GOOGL": (0.030, 0.050),
        }.get(ticker, (0.030, 0.050))
    if strategy_id == "us_equity_token_equity_momentum":
        return {
            "AMZN": (0.030, 0.050),
            "GOOGL": (0.030, 0.050),
            "NVDA": (0.030, 0.050),
        }.get(ticker, (0.030, 0.050))
    return (0.035, 0.0525)


def _equity_ret1(ticker: str) -> float | None:
    cached = _equity_ret1_from_cache(ticker)
    if cached is not None:
        return cached
    try:
        import yfinance as yf  # type: ignore

        hist = yf.download(ticker, period="10d", interval="1d", progress=False, auto_adjust=True)
    except Exception:
        return None
    if hist is None or len(hist) < 2:
        return None
    close = hist["Close"]
    if hasattr(close, "columns"):
        close = close.iloc[:, 0]
    close = close.dropna().astype(float)
    if len(close) < 2:
        return None
    return float(close.iloc[-1] / close.iloc[-2] - 1.0)


def _equity_ret1_from_cache(ticker: str) -> float | None:
    path = US_EQUITY_CACHE_DIR / f"{ticker.upper()}.csv"
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path, parse_dates=["date"]).set_index("date").sort_index()
    except Exception:
        return None
    if len(df) < 2 or "close" not in df:
        return None
    close = pd.to_numeric(df["close"], errors="coerce").dropna()
    if len(close) < 2:
        return None
    last_date = pd.Timestamp(close.index[-1]).date()
    age_days = (datetime.now(timezone.utc).date() - last_date).days
    if age_days > 7:
        return None
    return float(close.iloc[-1] / close.iloc[-2] - 1.0)


def _load_ohlcv(symbol: str, timeframe: str) -> pd.DataFrame:
    path = CACHE_DIR / f"{symbol}_futures_{timeframe}.parquet"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_parquet(path).copy()
    df.index = pd.to_datetime(df.index, utc=True)
    return df.sort_index().dropna(subset=["open", "high", "low", "close"])


def _gross_return(pos: Position, mark: float) -> float:
    raw = float(mark) / pos.entry_price - 1.0
    return raw if pos.side == "long" else -raw


def _pnl(pos: Position, mark: float) -> float:
    return pos.notional * _gross_return(pos, mark)


def _round_trip_cost_rate() -> float:
    return 2.0 * (DEFAULT_FEE_BPS_PER_SIDE + DEFAULT_SLIPPAGE_BPS_PER_SIDE) / 10000.0


def _min_net_profit_rate() -> float:
    return MIN_TARGET_NET_PROFIT_BPS / 10000.0


def _net_pnl(pos: Position, mark: float) -> float:
    return pos.notional * (_gross_return(pos, mark) - _round_trip_cost_rate())


def _fee_guard(pos: Position) -> dict[str, Any]:
    target_return = None
    target_net_return = None
    if pos.target is not None:
        target_return = _gross_return(pos, float(pos.target))
        target_net_return = target_return - _round_trip_cost_rate()
    return {
        "fee_model": {
            "fee_bps_per_side": DEFAULT_FEE_BPS_PER_SIDE,
            "slippage_bps_per_side": DEFAULT_SLIPPAGE_BPS_PER_SIDE,
            "round_trip_cost_rate": _round_trip_cost_rate(),
            "min_target_net_profit_rate": _min_net_profit_rate(),
        },
        "target_gross_return": target_return,
        "target_net_return_after_cost": target_net_return,
        "target_net_profitable": target_net_return is None or target_net_return >= _min_net_profit_rate(),
    }


def _event(
    ts: str,
    event: str,
    pos: Position,
    price: float,
    *,
    pnl: float | None = None,
    reason: str = "",
    live_order: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = {
        "ts": ts,
        "event": event,
        "symbol": pos.symbol,
        "side": pos.side,
        "price": price,
        "entry_price": pos.entry_price,
        "notional": pos.notional,
        "leverage": pos.leverage,
        "stop": pos.stop,
        "target": pos.target,
        "source_strategy_id": pos.source_strategy_id,
        "signal_family": pos.signal_family,
        "strategy_id": pos.source_strategy_id,
        "reason": reason,
        "thesis_contract": pos.thesis_contract,
    }
    if live_order is not None:
        row["live_order"] = live_order
    if pnl is not None:
        row["pnl"] = pnl
    return row


def _place_live_entry(args: argparse.Namespace, pos: Position) -> dict[str, Any]:
    decision = _decision_from_position(pos)
    instrument = _instrument_from_position(pos)
    candidate = candidate_trade_from_signal(decision.signal)
    plan = ApprovedTradePlan(
        decision_id=decision.decision_id,
        candidate=candidate,
        environment=args.environment,
        okx_profile=args.okx_profile,
        margin_usdt=pos.notional / max(1.0, pos.leverage),
        notional_usdt=pos.notional,
        leverage=pos.leverage,
        stop_price=pos.stop,
        target_price=pos.target,
        max_account_loss_usdt=abs(pos.notional * ((pos.stop / pos.entry_price) - 1.0)) if pos.entry_price > 0 else 0.0,
        approved_at=datetime.now(timezone.utc),
        risk_policy_id="research_sleeve_v1",
        metadata={"signal_family": pos.signal_family, "thesis_contract": pos.thesis_contract},
    )
    journal = _ownership_journal(args)
    journal.append_candidate(candidate, {"source": "research_sleeve"})
    journal.append_plan(plan)
    router = LiveExecutionRouter(config=ExecutionConfig(profile=args.okx_profile, default_leverage=pos.leverage))
    order = router.build_order(decision, instrument, router.config)
    if bool(getattr(args, "live_dry_run", False)):
        return {
            "dry_run": True,
            "source": "execution_router",
            "order_intent": _jsonable(vars(order)),
        }
    fill = router.execute(order, pos.entry_price)
    journal.append_execution(
        ExecutionReceipt(
            decision_id=decision.decision_id,
            environment=args.environment,
            okx_profile=args.okx_profile,
            inst_id=order.inst_id,
            status="filled" if fill.status == "filled" else ("rejected" if fill.status == "error" else "unknown"),
            submitted_at=order.timestamp,
            filled_at=fill.timestamp if fill.fill_size > 0 else None,
            order_ids={"ordId": fill.order_id} if fill.order_id else {},
            fill_price=fill.fill_price if fill.fill_price > 0 else None,
            filled_contracts=fill.fill_size,
            fee_usdt=fill.fee,
            raw=fill.raw,
        ),
        {"order_intent": vars(order)},
    )
    return {
        "source": "execution_router",
        "order_intent": _jsonable(vars(order)),
        "fill": _jsonable(vars(fill)),
        "ok": fill.status != "error",
        "error": fill.error,
    }


def _decision_from_position(pos: Position) -> Decision:
    now = datetime.fromisoformat(pos.entry_ts) if pos.entry_ts else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    signal = Signal(
        strategy_id=pos.source_strategy_id,
        symbol=pos.symbol,
        side=pos.side,
        timestamp=now,
        entry=pos.entry_price,
        target=pos.target,
        stop=pos.stop,
        horizon_sec=int(pos.thesis_contract.get("horizon_sec") or 12 * 3600),
        p_target=float(pos.thesis_contract.get("p_target") or pos.thesis_contract.get("confidence") or 0.5),
        adverse_pct_estimate=abs(pos.entry_price - pos.stop) / max(pos.entry_price, 1e-9),
        confidence=float(pos.thesis_contract.get("confidence") or 0.5),
        metadata={
            "signal_family": pos.signal_family,
            "leverage": pos.leverage,
            "thesis_contract": pos.thesis_contract,
        },
    )
    return Decision(
        signal=signal,
        size_usdt=pos.notional,
        reason=pos.signal_family,
        timestamp=now,
        metadata={"leverage": pos.leverage},
    )


def _instrument_from_position(pos: Position) -> InstrumentSpec:
    inst_id = pos.symbol.replace("_", "-") + "-SWAP"
    return InstrumentSpec(
        inst_id=inst_id,
        symbol=pos.symbol,
        ct_val=_contract_value(pos.symbol),
        lot_sz=0.01,
        min_sz=0.01,
        max_leverage=pos.leverage,
        source="research_sleeve_adapter",
    )


def _refresh_live_entry(args: argparse.Namespace, pos: Position) -> Position:
    old_entry = pos.entry_price
    live_entry = _live_ticker_last(args, pos.symbol)
    if live_entry <= 0 or not math.isfinite(live_entry):
        return pos
    if pos.side == "long":
        stop_pct = max(0.001, (old_entry - pos.stop) / old_entry)
        target_pct = max(0.001, ((pos.target or old_entry) - old_entry) / old_entry) if pos.target else None
        stop = live_entry * (1.0 - stop_pct)
        target = live_entry * (1.0 + target_pct) if target_pct is not None else None
    else:
        stop_pct = max(0.001, (pos.stop - old_entry) / old_entry)
        target_pct = max(0.001, (old_entry - (pos.target or old_entry)) / old_entry) if pos.target else None
        stop = live_entry * (1.0 + stop_pct)
        target = live_entry * (1.0 - target_pct) if target_pct is not None else None
    contract = dict(pos.thesis_contract)
    contract["signal_reference_price"] = old_entry
    contract["live_entry_price"] = live_entry
    return Position(
        symbol=pos.symbol,
        side=pos.side,
        entry_ts=pos.entry_ts,
        entry_price=live_entry,
        notional=pos.notional,
        leverage=pos.leverage,
        stop=stop,
        target=target,
        source_strategy_id=pos.source_strategy_id,
        signal_family=pos.signal_family,
        thesis_contract=contract,
    )


def _live_ticker_last(args: argparse.Namespace, symbol: str) -> float:
    inst_id = symbol.replace("_", "-") + "-SWAP"
    data = _call_okx(["market", "ticker", inst_id], args.okx_profile, json_output=True, retries=3)
    if isinstance(data, list) and data:
        data = data[0]
    if isinstance(data, dict):
        for key in ("last", "lastPx", "markPx", "idxPx"):
            if data.get(key) not in (None, ""):
                return float(data[key])
    return 0.0


def _live_open_symbols(args: argparse.Namespace) -> set[str]:
    return set(_live_position_rows(args))


def _live_position_rows(args: argparse.Namespace) -> dict[str, dict[str, Any]]:
    try:
        data = _call_okx(["account", "positions", "--instType", "SWAP"], args.okx_profile, json_output=True, retries=3)
    except Exception:
        data = _call_okx(["account", "positions"], args.okx_profile, json_output=True, retries=3)
    positions = data.get("data", data) if isinstance(data, dict) else data
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(positions, list):
        return out
    for row in positions:
        try:
            pos_size = abs(float(row.get("pos") or row.get("availPos") or 0.0))
        except Exception:
            pos_size = 0.0
        if pos_size <= 0:
            continue
        inst_id = str(row.get("instId") or "")
        if inst_id.endswith("-USDT-SWAP"):
            out[inst_id.removesuffix("-USDT-SWAP").replace("-", "_") + "_USDT"] = row
    return out


def _recover_live_positions_from_ledger(
    args: argparse.Namespace,
    state_id: str,
    live_position_rows: dict[str, dict[str, Any]],
    marks: dict[str, float],
    *,
    existing_symbols: set[str] | None = None,
) -> dict[str, Position]:
    existing_symbols = existing_symbols or set()
    ledger_paths = _candidate_recovery_ledgers(args, state_id)
    if not ledger_paths:
        return {}
    open_events: dict[str, dict[str, Any]] = {}
    for ledger_path in ledger_paths:
        for line in ledger_path.read_text().splitlines():
            try:
                event = json.loads(line)
            except Exception:
                continue
            symbol = str(event.get("symbol") or "")
            if not symbol:
                continue
            name = str(event.get("event") or "").lower()
            if name == "entry" and str(event.get("source_strategy_id") or event.get("strategy_id") or "") == args.strategy_id:
                if not _event_has_executed_live_order(event):
                    continue
                event = dict(event)
                event["_recovery_ledger"] = str(ledger_path.name)
                open_events[symbol] = event
            elif "exit" in name or name in {"manual_close", "flatten"}:
                open_events.pop(symbol, None)

    recovered: dict[str, Position] = {}
    for symbol, event in open_events.items():
        if symbol in existing_symbols:
            continue
        row = live_position_rows.get(symbol)
        if not row:
            continue
        avg_px = _float_or_none(row.get("avgPx")) or _float_or_none(event.get("entry_price")) or _float_or_none(event.get("price"))
        if avg_px is None or avg_px <= 0:
            continue
        pos_size = _float_or_none(row.get("pos")) or 0.0
        side = "long" if pos_size > 0 else "short"
        notional = abs(_float_or_none(row.get("notionalUsd")) or _float_or_none(event.get("notional")) or 0.0)
        leverage = _float_or_none(row.get("lever")) or _float_or_none(event.get("leverage")) or 1.0
        stop = _float_or_none(event.get("stop"))
        target = _float_or_none(event.get("target"))
        live_stop, live_target = _live_attached_stop_target(args, symbol)
        stop = live_stop or stop
        target = live_target if live_target is not None else target
        if stop is None or stop <= 0:
            ticker = symbol.split("_", 1)[0]
            stop_pct, target_pct = _stock_token_risk_params(str(args.strategy_id), ticker)
            stop = avg_px * (1.0 - stop_pct) if side == "long" else avg_px * (1.0 + stop_pct)
            if target is None:
                target = avg_px * (1.0 + target_pct) if side == "long" else avg_px * (1.0 - target_pct)
        marks.setdefault(symbol, _float_or_none(row.get("markPx")) or _float_or_none(row.get("last")) or avg_px)
        thesis = dict(event.get("thesis_contract") or {})
        thesis["recovered_from_live_position"] = True
        thesis["recovered_at"] = datetime.now(timezone.utc).isoformat()
        thesis["recovery_ledger"] = event.get("_recovery_ledger")
        thesis["recovery_environment"] = args.environment
        thesis["recovery_okx_profile"] = args.okx_profile
        recovered[symbol] = Position(
            symbol=symbol,
            side=side,
            entry_ts=str(event.get("ts") or row.get("cTime") or datetime.now(timezone.utc).isoformat()),
            entry_price=float(avg_px),
            notional=float(notional or abs(pos_size) * avg_px),
            leverage=float(leverage),
            stop=float(stop),
            target=float(target) if target is not None else None,
            source_strategy_id=args.strategy_id,
            signal_family=str(event.get("signal_family") or event.get("reason") or "recovered_live_position"),
            thesis_contract=thesis,
        )
    return recovered


def _event_has_executed_live_order(event: dict[str, Any]) -> bool:
    live_order = event.get("live_order")
    if isinstance(live_order, list):
        return any(str(item.get("sCode") or "") == "0" and item.get("ordId") for item in live_order if isinstance(item, dict))
    if isinstance(live_order, dict):
        if live_order.get("dry_run"):
            return False
        order = live_order.get("order")
        if isinstance(order, dict) and order.get("dry_run"):
            return False
        return bool(live_order.get("ordId") or live_order.get("ok"))
    return False


def _external_live_exit_from_bills(args: argparse.Namespace, pos: Position) -> tuple[float | None, float | None, str | None, str]:
    inst_id = pos.symbol.replace("_", "-") + "-SWAP"
    try:
        data = _call_okx(["account", "bills", "--instType", "SWAP"], args.okx_profile, json_output=True, retries=3)
    except Exception:
        return None, None, None, "live_position_missing"
    rows = data.get("data", data) if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return None, None, None, "live_position_missing"
    entry_ms = _epoch_ms(pos.entry_ts)
    best: dict[str, Any] | None = None
    for row in rows:
        if str(row.get("instId") or "") != inst_id:
            continue
        if str(row.get("type") or "") != "2":
            continue
        pnl = _float_or_none(row.get("pnl"))
        if pnl is None or abs(pnl) <= 1e-12:
            continue
        ts_ms = int(_float_or_none(row.get("ts")) or _float_or_none(row.get("fillTime")) or 0)
        if entry_ms is not None and ts_ms and ts_ms < entry_ms:
            continue
        if best is None or ts_ms > int(_float_or_none(best.get("ts")) or _float_or_none(best.get("fillTime")) or 0):
            best = row
    if not best:
        return None, None, None, "live_position_missing"
    price = _float_or_none(best.get("px")) or _float_or_none(best.get("fillMarkPx")) or _float_or_none(best.get("fillIdxPx"))
    pnl = _float_or_none(best.get("pnl"))
    fee = _float_or_none(best.get("fee")) or 0.0
    ts = _iso_from_epoch_ms(best.get("ts") or best.get("fillTime"))
    reason = "external_stop_or_target"
    if price is not None:
        if pos.side == "long" and price <= pos.stop * 1.002:
            reason = "external_stop"
        elif pos.side == "short" and price >= pos.stop * 0.998:
            reason = "external_stop"
        elif pos.target is not None and pos.side == "long" and price >= pos.target * 0.998:
            reason = "external_target"
        elif pos.target is not None and pos.side == "short" and price <= pos.target * 1.002:
            reason = "external_target"
    return price, (pnl + fee if pnl is not None else None), ts, reason


def _epoch_ms(value: str) -> int | None:
    try:
        return int(pd.Timestamp(value).timestamp() * 1000)
    except Exception:
        return None


def _iso_from_epoch_ms(value: Any) -> str | None:
    parsed = _float_or_none(value)
    if parsed is None or parsed <= 0:
        return None
    return datetime.fromtimestamp(parsed / 1000.0, tz=timezone.utc).isoformat()


def _candidate_recovery_ledgers(args: argparse.Namespace, state_id: str) -> list[Path]:
    primary = LOG_DIR / f"{state_id}_{args.environment}_ledger.jsonl"
    paths: list[Path] = []
    if primary.exists():
        paths.append(primary)
    # Recover orphaned live positions created by an older environment/profile mismatch.
    # The live position rows still come from the current validated OKX profile, so this
    # only imports state for orders that actually exist in the active account.
    for path in sorted(LOG_DIR.glob(f"{state_id}_*_ledger.jsonl"), key=lambda item: item.stat().st_mtime, reverse=True):
        if path not in paths:
            paths.append(path)
    return paths


def _live_attached_stop_target(args: argparse.Namespace, symbol: str) -> tuple[float | None, float | None]:
    inst_id = symbol.replace("_", "-") + "-SWAP"
    try:
        data = _call_okx(["swap", "algo", "orders", "--instId", inst_id], args.okx_profile, json_output=True, retries=3)
    except Exception:
        return None, None
    rows = data.get("data", data) if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return None, None
    stop = None
    target = None
    for row in rows:
        if str(row.get("state") or "").lower() != "live":
            continue
        if stop is None:
            stop = _float_or_none(row.get("slTriggerPx"))
        if target is None:
            target = _float_or_none(row.get("tpTriggerPx"))
        if stop is not None and target is not None:
            break
    return stop, target


def _float_or_none(value: Any) -> float | None:
    try:
        parsed = float(value)
    except Exception:
        return None
    return parsed if math.isfinite(parsed) else None


def _close_live_position(args: argparse.Namespace, pos: Position) -> dict[str, Any]:
    inst_id = pos.symbol.replace("_", "-") + "-SWAP"
    if bool(getattr(args, "live_dry_run", False)):
        return {
            "dry_run": True,
            "source": "kit_execution_gateway",
            "action": "close_position",
            "inst_id": inst_id,
            "profile": args.okx_profile,
        }
    gateway = KitExecutionGateway(
        KitClient(KitClientConfig(default_profile=args.okx_profile, live_enabled=os.environ.get("LIVE_TRADING", "false").lower() == "true")),
        profile=args.okx_profile,
        allow_live=os.environ.get("LIVE_TRADING", "false").lower() == "true",
    )
    result = gateway.close_position(inst_id, mgn_mode="cross", pos_side="net", profile=args.okx_profile)
    row = {
        "source": "kit_execution_gateway",
        "ok": result.ok,
        "argv": result.argv,
        "data": result.data,
        "error": result.error,
    }
    _ownership_journal(args).append_close(
        strategy_id=pos.source_strategy_id,
        inst_id=inst_id,
        reason="research_sleeve_close",
        result=row,
        metadata={"symbol": pos.symbol, "signal_family": pos.signal_family},
    )
    return row


def _ownership_journal(args: argparse.Namespace) -> LiveOwnershipJournal:
    return LiveOwnershipJournal.from_engine_dir(ENGINE_DIR, args.environment, args.okx_profile)


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _order_size(pos: Position) -> str:
    qty = max(0.0, pos.notional / max(1e-9, pos.entry_price * _contract_value(pos.symbol)))
    lots = math.floor(qty / 0.01) * 0.01
    lots = max(0.01, lots)
    return f"{lots:.2f}".rstrip("0").rstrip(".")


def _contract_value(symbol: str) -> float:
    return {
        "BTC_USDT": 0.01,
        "ETH_USDT": 0.1,
    }.get(symbol, 1.0)


def _px(value: float) -> str:
    return f"{float(value):.4f}".rstrip("0").rstrip(".")


def _call_okx(args: list[str], profile: str, json_output: bool = False, retries: int = 1) -> dict[str, Any]:
    if _is_okx_trade_command(args):
        raise RuntimeError("research sleeve adapter may not issue OKX trade commands directly; use execution layer")
    cmd = ["okx", "--profile", profile]
    if json_output:
        cmd.append("--json")
    cmd.extend(args)
    last_error = ""
    for attempt in range(max(1, int(retries))):
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=_okx_cli_env(profile))
        if result.returncode == 0:
            output = result.stdout.strip()
            if json_output and output:
                return json.loads(output)
            return {"ok": True, "output": output}
        last_error = result.stderr.strip() or result.stdout.strip()
        if attempt + 1 < max(1, int(retries)):
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(last_error)


def _is_okx_trade_command(args: list[str]) -> bool:
    if len(args) < 2:
        return False
    module = str(args[0])
    action = str(args[1])
    if module != "swap":
        return False
    return action not in {"positions", "orders", "get", "fills", "algo"}


def _dry_run_okx(args: list[str], profile: str, json_output: bool = False) -> dict[str, Any]:
    cmd = ["okx", "--profile", profile]
    if json_output:
        cmd.append("--json")
    cmd.extend(args)
    return {"dry_run": True, "cmd": cmd}


def _okx_cli_env(profile: str) -> dict[str, str]:
    env = os.environ.copy()
    # OKX CLI gives environment credentials precedence over named profiles.
    # Keep live able to use the ambient competition credentials, but isolate
    # personal/demo so --profile cannot accidentally resolve to live keys.
    if str(profile) != "live":
        for key in OKX_ENV_CREDENTIAL_KEYS:
            env.pop(key, None)
    return env


def _append_outputs(args: argparse.Namespace, state_id: str, state: dict[str, Any]) -> None:
    prefix = f"{state_id}_{args.environment}"
    state_path = LOG_DIR / f"{prefix}.json"
    ledger_path = LOG_DIR / f"{prefix}_ledger.jsonl"
    equity_path = LOG_DIR / f"{prefix}_equity.jsonl"
    state_path.write_text(json.dumps({k: v for k, v in state.items() if k != "ledger_events"}, indent=2, sort_keys=True))
    with ledger_path.open("a") as fh:
        for event in state.get("ledger_events") or []:
            fh.write(json.dumps(event, sort_keys=True) + "\n")
    with equity_path.open("a") as fh:
        fh.write(json.dumps({"ts": state["updated_at"], "nav": state["nav"], "realized_nav": state["realized_nav"], "open_positions": len(state.get("positions") or {})}, sort_keys=True) + "\n")


def _load_state(state_id: str, environment: str) -> dict[str, Any]:
    path = LOG_DIR / f"{state_id}_{environment}.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _write_scheduler(args: argparse.Namespace, state_id: str, status: str, cycles: int, extra: dict[str, Any] | None = None) -> None:
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "strategy_id": args.strategy_id,
        "state_id": state_id,
        "environment": args.environment,
        "execution": args.execution,
        "okx_profile": args.okx_profile if args.execution == "live" else None,
        "status": status,
        "cycles": cycles,
    }
    if extra:
        payload.update(extra)
    (LOG_DIR / f"{state_id}_{args.environment}_scheduler.json").write_text(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
