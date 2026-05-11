#!/usr/bin/env python3
"""Local web launcher for the OKX trading system.

This server intentionally stays thin: it validates UI requests, then delegates
all trading lifecycle work to the existing local shell scripts.
"""

from __future__ import annotations

import argparse
import json
import math
import mimetypes
import os
import shutil
import signal
import site
import subprocess
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ROOT_DIR = Path(__file__).resolve().parents[1]
ENGINE_DIR = ROOT_DIR / "engine"
sys.path.insert(0, str(ENGINE_DIR))
STATIC_DIR = Path(__file__).resolve().parent / "static"
CONTROL_DIR = ROOT_DIR / "engine" / "control"
LOGS_DIR = ROOT_DIR / "engine" / "logs"
PRO_PAPER_DIR = LOGS_DIR / "pro_paper"
C_AUTO_V2_PAPER_DIR = LOGS_DIR / "c_auto_v2_paper"
C_AUTO_V2_MICRO_LIVE_DIR = LOGS_DIR / "c_auto_v2_micro_live"
TRAINING_HISTORY_DIR = ROOT_DIR / "engine" / "data" / "training_history"
DERIVATIVES_STRUCTURE_DIR = ROOT_DIR / "engine" / "data" / "derivatives_structure"
MONSTER_EVENTS_DIR = ROOT_DIR / "engine" / "data" / "monster_events"
MONSTER_PAPER_DIR = LOGS_DIR / "monster_paper"
DATA_REFRESH_DIR = LOGS_DIR / "data_refresh"
SMARTMONEY_DIFFUSION_DIR = LOGS_DIR / "smartmoney_diffusion"
PYTHON_BIN = os.environ.get("OKX_TRADING_SYSTEM_PYTHON", sys.executable)
KILL_SWITCH_PATH = CONTROL_DIR / "kill.switch"

from registry import StrategyRegistry


ALLOWED_ENVS = {"personal", "demo", "competition"}
ALLOWED_MODES = {"paper", "real"}
LEGACY_STRATEGIES = {"elite_flow", "yolo_momentum", "yolo_orchestrator"}
DEFAULT_DASHBOARD_PORT = 8080
DEFAULT_DOWNLOAD_RUN_ID = "train_hist_vol1m_1h_20240101_20260424"
DEFAULT_DERIVATIVES_RUN_ID = "deriv_struct_132_5m_20240101_20260424"
DEFAULT_MONSTER_WATCHLIST_ID = "monster_watchlist_5m_live_gated_20260426"
C_AUTO_V2_STRATEGY_ID = "c_auto_v2_fixed1000_conservative"
C_AUTO_V2_STATE_ID = "fixed1000_conservative"
C_AUTO_V2_CAPITAL_USDT = 3000.0


def read_text(path: Path) -> str | None:
    try:
        return path.read_text().strip() if path.exists() else None
    except OSError:
        return None


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text()) if path.exists() else None
    except Exception:
        return None


def json_safe(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [json_safe(item) for item in value]
    return value


def process_alive(pid: str | int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def pid_snapshot() -> dict[str, Any]:
    strategies = []
    for strategy in sorted(LEGACY_STRATEGIES):
        for env in ("demo", "live", "personal"):
            path = CONTROL_DIR / f"{strategy}_{env}.pid"
            pid = read_text(path)
            if pid:
                strategies.append(
                    {
                        "strategy": strategy,
                        "env": env,
                        "pid": pid,
                        "alive": process_alive(pid),
                        "pid_file": str(path.relative_to(ROOT_DIR)),
                    }
                )

    dashboard_pid = read_text(CONTROL_DIR / "dashboard.pid")
    launcher_pid = read_text(CONTROL_DIR / "launcher.pid")
    return {
        "dashboard": {
            "pid": dashboard_pid,
            "alive": process_alive(dashboard_pid),
            "pid_file": "engine/control/dashboard.pid",
        },
        "launcher": {
            "pid": launcher_pid,
            "alive": process_alive(launcher_pid),
            "pid_file": "engine/control/launcher.pid",
        },
        "strategies": strategies,
        "pro_paper": find_pro_paper_processes(),
        "c_auto_v2_paper": find_c_auto_v2_paper_processes(),
        "c_auto_v2_micro_live": find_c_auto_v2_micro_live_processes(),
        "data_refresh": find_data_refresh_processes(),
        "smartmoney_diffusion": find_smartmoney_diffusion_processes(),
        "c_auto_daily_review": find_c_auto_daily_review_processes(),
    }


def active_summary() -> dict[str, Any]:
    summary = read_json(ROOT_DIR / "engine" / "logs" / "summary.json") or {}
    if not summary:
        return {}
    pid = summary.get("pid")
    if process_alive(pid):
        return summary
    stale = dict(summary)
    stale["engine_status"] = "stale"
    stale["stale"] = True
    stale["stale_reason"] = f"pid {pid} is not running"
    stale["portfolios"] = {}
    stale["total_nav"] = 0.0
    stale["total_capital"] = 0.0
    stale["total_pnl"] = 0.0
    stale["total_pnl_pct"] = 0.0
    return stale


def strategy_options() -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = [
        {
            "strategy_id": C_AUTO_V2_STRATEGY_ID,
            "name": "C-Auto v2 Fixed1000 Conservative",
            "book": "core",
            "status": "paper-candidate",
            "kind": "c_auto_v2",
            "description": "BTC regime + alt rank + high-beta booster with parallel paper and 50U/day micro-live validation.",
            "live_enabled": True,
            "live_allocation_pct": 0.0167,
            "default_parameter_set_id": "c_auto_v2.fixed1000_conservative",
            "paper_supported": True,
            "real_supported": True,
            "primary": True,
        }
    ]
    try:
        registry = StrategyRegistry()
        for record in registry.list_strategies():
            options.append(
                {
                    "strategy_id": record.strategy_id,
                    "name": record.name,
                    "book": record.book,
                    "status": record.status,
                    "kind": "professional",
                    "description": record.description,
                    "live_enabled": record.live_enabled,
                    "live_allocation_pct": record.live_allocation_pct,
                    "default_parameter_set_id": record.default_parameter_set_id,
                    "paper_supported": True,
                    "real_supported": bool(record.live_enabled and record.status == "live"),
                    "primary": False,
                }
            )
    except Exception:
        pass
    for strategy in sorted(LEGACY_STRATEGIES):
        options.append(
            {
                "strategy_id": strategy,
                "name": strategy.replace("_", " ").title(),
                "book": "legacy",
                "status": "legacy",
                "kind": "legacy",
                "description": "Legacy competition launcher path",
                "live_enabled": False,
                "live_allocation_pct": 0.0,
                "paper_supported": False,
                "real_supported": True,
                "primary": False,
            }
        )
    return options


def strategy_option(strategy_id: str) -> dict[str, Any] | None:
    for item in strategy_options():
        if item["strategy_id"] == strategy_id:
            return item
    return None


def tail_file(path: Path, lines: int = 120) -> list[str]:
    if not path.exists():
        return []
    try:
        data = path.read_text(errors="replace").splitlines()
        return data[-lines:]
    except OSError:
        return []


def latest_launcher_logs() -> list[dict[str, Any]]:
    items = []
    for path in sorted(LOGS_DIR.glob("launcher_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:5]:
        items.append(
            {
                "path": str(path.relative_to(ROOT_DIR)),
                "mtime": path.stat().st_mtime,
                "tail": tail_file(path, 80),
            }
        )
    return items


def find_pro_paper_processes(strategy_id: str | None = None) -> list[dict[str, Any]]:
    try:
        proc = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            cwd=str(ROOT_DIR),
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    matches: list[dict[str, Any]] = []
    self_pid = os.getpid()
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if not stripped or "scripts/start_pro_paper.py" not in stripped:
            continue
        try:
            pid_raw, command = stripped.split(None, 1)
            pid = int(pid_raw)
        except ValueError:
            continue
        if pid == self_pid:
            continue
        found_strategy = _command_arg(command, "--strategy-id")
        found_env = _command_arg(command, "--environment")
        if strategy_id and found_strategy != strategy_id:
            continue
        matches.append({"pid": pid, "command": command, "strategy_id": found_strategy, "environment": found_env})
    return matches


def find_c_auto_v2_paper_processes(state_id: str | None = None) -> list[dict[str, Any]]:
    try:
        proc = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            cwd=str(ROOT_DIR),
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    matches: list[dict[str, Any]] = []
    self_pid = os.getpid()
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if not stripped or "scripts/run_c_auto_v2_paper.py" not in stripped:
            continue
        try:
            pid_raw, command = stripped.split(None, 1)
            pid = int(pid_raw)
        except ValueError:
            continue
        if pid == self_pid:
            continue
        found_state = _command_arg(command, "--state-id")
        found_env = _command_arg(command, "--environment")
        source_mode = _command_arg(command, "--source-mode") or "replay"
        if state_id and found_state != state_id:
            continue
        matches.append(
            {
                "pid": pid,
                "command": command,
                "state_id": found_state,
                "environment": found_env,
                "source_mode": source_mode,
            }
        )
    return matches


def find_c_auto_v2_micro_live_processes(state_id: str | None = None) -> list[dict[str, Any]]:
    try:
        proc = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            cwd=str(ROOT_DIR),
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    matches: list[dict[str, Any]] = []
    self_pid = os.getpid()
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if not stripped or "scripts/run_c_auto_v2_micro_live.py" not in stripped:
            continue
        try:
            pid_raw, command = stripped.split(None, 1)
            pid = int(pid_raw)
        except ValueError:
            continue
        if pid == self_pid:
            continue
        found_state = _command_arg(command, "--state-id")
        found_env = _command_arg(command, "--environment")
        if state_id and found_state != state_id:
            continue
        matches.append(
            {
                "pid": pid,
                "command": command,
                "state_id": found_state,
                "environment": found_env,
                "source_mode": "micro_live",
            }
        )
    return matches


def find_c_auto_daily_review_processes() -> list[dict[str, Any]]:
    try:
        proc = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            cwd=str(ROOT_DIR),
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    matches: list[dict[str, Any]] = []
    self_pid = os.getpid()
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if not stripped or "scripts/run_c_auto_daily_review_scheduler.py" not in stripped:
            continue
        try:
            pid_raw, command = stripped.split(None, 1)
            pid = int(pid_raw)
        except ValueError:
            continue
        if pid == self_pid:
            continue
        matches.append(
            {
                "pid": pid,
                "command": command,
                "state_id": _command_arg(command, "--state-id"),
                "environment": _command_arg(command, "--environment"),
            }
        )
    return matches


def _command_arg(command: str, key: str) -> str | None:
    parts = command.split()
    for i, part in enumerate(parts):
        if part == key and i + 1 < len(parts):
            return parts[i + 1]
        if part.startswith(f"{key}="):
            return part.split("=", 1)[1]
    return None


def pro_paper_status(strategy_id: str = "core_c_auto_h24_regression_v1", environment: str = "personal") -> dict[str, Any]:
    status_path = PRO_PAPER_DIR / f"{strategy_id}_{environment}.json"
    scheduler_path = PRO_PAPER_DIR / f"{strategy_id}_{environment}_scheduler.json"
    status = read_json(status_path) or {}
    scheduler = read_json(scheduler_path) or {}
    processes = find_pro_paper_processes(strategy_id)
    return {
        "available": bool(status or scheduler or processes),
        "strategy_id": strategy_id,
        "environment": environment,
        "running": bool(processes),
        "processes": processes,
        "status": status,
        "scheduler": scheduler,
        "updated_at": scheduler.get("heartbeat_at") or status.get("heartbeat_at") or status.get("timestamp"),
        "status_path": str(status_path.relative_to(ROOT_DIR)),
        "scheduler_status_path": str(scheduler_path.relative_to(ROOT_DIR)),
    }


def start_pro_paper(strategy_id: str, environment: str) -> dict[str, Any]:
    existing = find_pro_paper_processes(strategy_id)
    if existing:
        return {"ok": True, "already_running": True, "processes": existing, "status": pro_paper_status(strategy_id, environment)}
    stop_path = CONTROL_DIR / f"pro_paper_{strategy_id}_{environment}.stop"
    try:
        stop_path.unlink()
    except FileNotFoundError:
        pass
    result = run_script(
        [
            "python3",
            "scripts/start_pro_paper.py",
            "--strategy-id",
            strategy_id,
            "--environment",
            environment,
            "--symbols",
            "BTC/USDT,ETH/USDT,SOL/USDT",
            "--timeframe",
            "1h",
            "--interval-sec",
            "60",
            "--max-cycles",
            "0",
            "--warmup-bars",
            "720",
        ],
        f"pro_paper_{strategy_id}_{environment}",
    )
    result.update({"ok": True, "strategy": strategy_id, "environment": environment, "mode": "paper"})
    return result


def c_auto_v2_paper_status(state_id: str = "fixed1000_conservative", environment: str | None = None) -> dict[str, Any]:
    processes = find_c_auto_v2_paper_processes(state_id)
    if environment is None:
        live_envs = [str(proc.get("environment") or "") for proc in processes if proc.get("source_mode") == "live"]
        environment = next((env for env in live_envs if env in ALLOWED_ENVS), None)
    if environment is None:
        candidates = sorted(C_AUTO_V2_PAPER_DIR.glob(f"{state_id}_*_scheduler.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in candidates:
            suffix = path.name.removeprefix(f"{state_id}_").removesuffix("_scheduler.json")
            if suffix in ALLOWED_ENVS:
                environment = suffix
                break
    environment = environment or "personal"
    prefix = f"{state_id}_{environment}"
    state_path = C_AUTO_V2_PAPER_DIR / f"{prefix}.json"
    scheduler_path = C_AUTO_V2_PAPER_DIR / f"{prefix}_scheduler.json"
    state = read_json(state_path) or {}
    scheduler = read_json(scheduler_path) or {}
    if not state and not scheduler and not processes:
        return {"available": False, "state_id": state_id, "message": "no c-auto v2 paper state found"}
    display_scheduler = dict(scheduler)
    if not processes and display_scheduler.get("scheduler_status") == "running":
        display_scheduler["scheduler_status"] = str(state.get("runner_status") or "stopped")
    out = dict(state)
    out.update(
        {
            "available": True,
            "running": bool(processes),
            "processes": processes,
            "scheduler": display_scheduler,
            "state_path": str(state_path.relative_to(ROOT_DIR)) if state_path.exists() else None,
            "scheduler_status_path": str(scheduler_path.relative_to(ROOT_DIR)) if scheduler_path.exists() else None,
        }
    )
    return out


def c_auto_v2_micro_live_status(state_id: str = "micro_live_competition", environment: str = "competition") -> dict[str, Any]:
    processes = find_c_auto_v2_micro_live_processes(state_id)
    prefix = f"{state_id}_{environment}"
    state_path = C_AUTO_V2_MICRO_LIVE_DIR / f"{prefix}.json"
    scheduler_path = C_AUTO_V2_MICRO_LIVE_DIR / f"{prefix}_scheduler.json"
    state = read_json(state_path) or {}
    scheduler = read_json(scheduler_path) or {}
    if not state and not scheduler and not processes:
        return {"available": False, "state_id": state_id, "message": "no c-auto v2 micro-live state found"}
    display_scheduler = dict(scheduler)
    if not processes and display_scheduler.get("scheduler_status") == "running":
        display_scheduler["scheduler_status"] = str(state.get("runner_status") or "stopped")
    out = dict(state)
    out.update(
        {
            "available": True,
            "running": bool(processes),
            "processes": processes,
            "scheduler": display_scheduler,
            "state_path": str(state_path.relative_to(ROOT_DIR)) if state_path.exists() else None,
            "scheduler_status_path": str(scheduler_path.relative_to(ROOT_DIR)) if scheduler_path.exists() else None,
        }
    )
    return out


def eight_layer_pipeline_status() -> dict[str, Any]:
    try:
        proc = subprocess.run(
            [PYTHON_BIN, "scripts/evaluate_8_layer_pipeline.py", "--json"],
            cwd=str(ROOT_DIR),
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    if proc.returncode != 0:
        return {"ok": False, "error": (proc.stderr or proc.stdout).strip()}
    try:
        return {"ok": True, **json.loads(proc.stdout)}
    except Exception as exc:
        return {"ok": False, "error": f"invalid pipeline status json: {exc}", "raw": proc.stdout[-2000:]}


def close_c_auto_v2_symbol(payload: dict[str, Any]) -> dict[str, Any]:
    state_id = str(payload.get("state_id") or "fixed1000_conservative").strip()
    environment = str(payload.get("environment") or payload.get("env") or "").strip()
    symbol = str(payload.get("symbol") or "").strip().upper()
    if not symbol:
        raise ValueError("symbol is required")
    if environment not in ALLOWED_ENVS:
        status = c_auto_v2_paper_status(state_id)
        environment = str(status.get("environment") or "personal")
    prefix = f"{state_id}_{environment}"
    state_path = C_AUTO_V2_PAPER_DIR / f"{prefix}.json"
    state = read_json(state_path) or {}
    positions = dict(state.get("positions") or {})
    matched_symbol = next((name for name in positions if name.upper() == symbol), None)
    if not matched_symbol:
        return {"ok": True, "closed": False, "symbol": symbol, "message": "symbol not open", "status": c_auto_v2_paper_status(state_id, environment)}

    pos = dict(positions.pop(matched_symbol) or {})
    mode = str(state.get("mode") or "paper").strip()
    exchange_result = None
    if mode in {"real", "live", "production"}:
        if not bool(payload.get("confirm_live_close")):
            raise ValueError("live close requires confirm_live_close=true")
        exchange_result = _close_c_auto_live_symbol(matched_symbol, environment)
        if not exchange_result.get("ok"):
            raise RuntimeError(exchange_result.get("message") or "live close failed")

    pnl = _position_unrealized_pnl(pos)
    net_return = _position_net_return(pos)
    realized_nav = float(state.get("realized_nav") or state.get("cash") or 1000.0) + pnl
    now = datetime.now(timezone.utc).isoformat()
    event = {
        "ts": now,
        "event": "manual_exit",
        "symbol": matched_symbol,
        "side": pos.get("side"),
        "reason": "launcher_one_click_close",
        "pnl": pnl,
        "net_return": net_return,
        "exit_price": pos.get("mark_price"),
        "mode": mode,
        "exchange_result": exchange_result,
    }
    ledger_tail = list(state.get("ledger_tail") or [])
    ledger_tail.append(event)
    state["positions"] = positions
    state["ledger_tail"] = ledger_tail[-40:]
    state["realized_nav"] = realized_nav
    state["cash"] = realized_nav
    state["realized_pnl"] = realized_nav - 1000.0
    state["unrealized_pnl"] = sum(_position_unrealized_pnl(dict(p)) for p in positions.values())
    state["nav"] = realized_nav + float(state["unrealized_pnl"])
    state["open_risk"] = sum(float(dict(p).get("risk_budget") or 0.0) for p in positions.values())
    state["updated_at"] = now
    C_AUTO_V2_PAPER_DIR.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True))
    with (C_AUTO_V2_PAPER_DIR / f"{prefix}_ledger.jsonl").open("a") as fh:
        fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    return {"ok": True, "closed": True, "symbol": matched_symbol, "mode": mode, "event": event, "exchange_result": exchange_result, "status": c_auto_v2_paper_status(state_id, environment)}


def _close_c_auto_live_symbol(symbol: str, environment: str) -> dict[str, Any]:
    profile = "live" if environment == "competition" else environment
    inst_id = _symbol_to_swap_inst_id(symbol)
    cancel_result = _cancel_profile_symbol_open_orders(profile, inst_id)
    close_result = _run_okx_json(["okx", "--profile", profile, "--json", "swap", "close", "--instId", inst_id, "--mgnMode", "cross", "--posSide", "net"])
    return {
        "ok": close_result["returncode"] == 0,
        "profile": profile,
        "inst_id": inst_id,
        "cancel_orders": cancel_result,
        "close_position": close_result,
        "message": close_result["message"],
    }


def close_c_auto_v2_micro_live_symbol(payload: dict[str, Any]) -> dict[str, Any]:
    state_id = str(payload.get("state_id") or "micro_live_competition").strip()
    environment = str(payload.get("environment") or payload.get("env") or "competition").strip()
    symbol = str(payload.get("symbol") or "").strip().upper()
    if not symbol:
        raise ValueError("symbol is required")
    if environment != "competition":
        raise ValueError("micro-live close is only enabled for competition environment")
    if not bool(payload.get("confirm_live_close")):
        raise ValueError("live close requires confirm_live_close=true")

    prefix = f"{state_id}_{environment}"
    state_path = C_AUTO_V2_MICRO_LIVE_DIR / f"{prefix}.json"
    state = read_json(state_path) or {}
    positions = dict(state.get("positions") or {})
    matched_symbol = next((name for name in positions if name.upper() == symbol), None)
    if not matched_symbol:
        return {"ok": True, "closed": False, "symbol": symbol, "message": "symbol not open", "status": c_auto_v2_micro_live_status(state_id, environment)}

    pos = dict(positions.pop(matched_symbol) or {})
    inst_id = str(pos.get("inst_id") or _symbol_to_swap_inst_id(matched_symbol))
    cancel_orders = _cancel_profile_symbol_open_orders("live", inst_id)
    cancel_algos = _cancel_profile_symbol_algo_orders("live", inst_id)
    close_result = _run_okx_json(["okx", "--profile", "live", "--json", "swap", "close", "--instId", inst_id, "--mgnMode", "isolated", "--posSide", "net", "--autoCxl"])
    if close_result["returncode"] != 0:
        raise RuntimeError(close_result["message"] or "micro-live close failed")

    pnl = _position_unrealized_pnl(pos)
    now = datetime.now(timezone.utc).isoformat()
    event = {
        "ts": now,
        "event": "manual_exit",
        "symbol": matched_symbol,
        "side": pos.get("side"),
        "reason": "launcher_micro_live_one_click_close",
        "pnl": pnl,
        "net_return": _position_net_return(pos),
        "exit_price": pos.get("mark_price"),
        "exchange_result": {
            "close_position": close_result,
            "cancel_orders": cancel_orders,
            "cancel_algo_orders": cancel_algos,
        },
    }
    ledger_tail = list(state.get("ledger_tail") or [])
    ledger_tail.append(event)
    state["positions"] = positions
    state["ledger_tail"] = ledger_tail[-80:]
    state["realized_pnl"] = float(state.get("realized_pnl") or 0.0) + pnl
    state["unrealized_pnl"] = sum(_position_unrealized_pnl(dict(p)) for p in positions.values())
    base_nav = float(state.get("daily_budget_usdt") or state.get("cash") or 50.0)
    state["nav"] = base_nav + float(state["realized_pnl"]) + float(state["unrealized_pnl"])
    state["open_risk"] = sum(float(dict(p).get("risk_budget") or 0.0) for p in positions.values())
    state["updated_at"] = now
    C_AUTO_V2_MICRO_LIVE_DIR.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True))
    with (C_AUTO_V2_MICRO_LIVE_DIR / f"{prefix}_ledger.jsonl").open("a") as fh:
        fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    with (C_AUTO_V2_MICRO_LIVE_DIR / f"{prefix}_equity.jsonl").open("a") as fh:
        fh.write(json.dumps({"ts": now, "nav": state["nav"], "open_positions": len(positions)}, ensure_ascii=False) + "\n")
    return {"ok": True, "closed": True, "symbol": matched_symbol, "event": event, "status": c_auto_v2_micro_live_status(state_id, environment)}


def close_monster_paper_symbol(payload: dict[str, Any]) -> dict[str, Any]:
    state_id = str(payload.get("state_id") or "lottery_live").strip()
    symbol = str(payload.get("symbol") or "").strip().upper()
    if not symbol:
        raise ValueError("symbol is required")
    state_path = MONSTER_PAPER_DIR / f"{state_id}.json"
    state = read_json(state_path) or {}
    positions = dict(state.get("positions") or {})
    matched_symbol = next((name for name in positions if name.upper() == symbol), None)
    if not matched_symbol:
        return {"ok": True, "closed": False, "symbol": symbol, "message": "symbol not open", "status": monster_paper_status(state_id)}
    pos = dict(positions.pop(matched_symbol) or {})
    pnl = _position_unrealized_pnl(pos)
    now = datetime.now(timezone.utc).isoformat()
    event = {
        "ts": now,
        "event": "manual_exit",
        "symbol": matched_symbol,
        "side": pos.get("side"),
        "reason": "launcher_monster_one_click_close",
        "pnl": pnl,
        "net_return": _position_net_return(pos),
        "exit_price": pos.get("mark_price"),
    }
    state["positions"] = positions
    state["cash"] = float(state.get("cash") or state.get("nav") or 1000.0) + pnl
    state["realized_pnl"] = float(state.get("realized_pnl") or 0.0) + pnl
    state["unrealized_pnl"] = sum(_position_unrealized_pnl(dict(p)) for p in positions.values())
    state["nav"] = float(state["cash"]) + float(state["unrealized_pnl"])
    state["updated_at"] = now
    MONSTER_PAPER_DIR.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True))
    with (MONSTER_PAPER_DIR / f"{state_id}_ledger.jsonl").open("a") as fh:
        fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    with (MONSTER_PAPER_DIR / f"{state_id}_equity.jsonl").open("a") as fh:
        fh.write(json.dumps({"ts": now, "nav": state["nav"], "open_positions": len(positions)}, ensure_ascii=False) + "\n")
    return {"ok": True, "closed": True, "symbol": matched_symbol, "event": event, "status": monster_paper_status(state_id)}


def _cancel_profile_symbol_open_orders(profile: str, inst_id: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "profile": profile,
        "inst_id": inst_id,
        "orders_found": 0,
        "orders_cancelled": 0,
        "orders_failed": 0,
        "errors": [],
        "cancelled": [],
    }
    resp = _run_okx_json(["okx", "--profile", profile, "--json", "swap", "orders", "--instId", inst_id, "--status", "open"])
    if resp["returncode"] != 0:
        result["errors"].append(resp["message"])
        return result
    orders = _as_order_list(resp["data"])
    result["orders_found"] = len(orders)
    for order in orders:
        ord_id = str(order.get("ordId") or order.get("ord_id") or order.get("orderId") or "").strip()
        if not ord_id:
            result["orders_failed"] += 1
            result["errors"].append(f"missing ordId in order: {order}")
            continue
        cancel = _run_okx_json(["okx", "--profile", profile, "swap", "cancel", inst_id, "--ordId", ord_id])
        if cancel["returncode"] == 0:
            result["orders_cancelled"] += 1
            result["cancelled"].append({"instId": inst_id, "ordId": ord_id})
        else:
            result["orders_failed"] += 1
            result["errors"].append(cancel["message"])
    return result


def _cancel_profile_symbol_algo_orders(profile: str, inst_id: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "profile": profile,
        "inst_id": inst_id,
        "orders_found": 0,
        "orders_cancelled": 0,
        "orders_failed": 0,
        "errors": [],
        "cancelled": [],
    }
    resp = _run_okx_json(["okx", "--profile", profile, "--json", "swap", "algo", "orders", "--instId", inst_id])
    if resp["returncode"] != 0:
        result["errors"].append(resp["message"])
        return result
    orders = _as_order_list(resp["data"])
    result["orders_found"] = len(orders)
    for order in orders:
        algo_id = str(order.get("algoId") or order.get("algo_id") or "").strip()
        if not algo_id:
            result["orders_failed"] += 1
            result["errors"].append(f"missing algoId in order: {order}")
            continue
        cancel = _run_okx_json(["okx", "--profile", profile, "--json", "swap", "algo", "cancel", "--instId", inst_id, "--algoId", algo_id])
        if cancel["returncode"] == 0:
            result["orders_cancelled"] += 1
            result["cancelled"].append({"instId": inst_id, "algoId": algo_id})
        else:
            result["orders_failed"] += 1
            result["errors"].append(cancel["message"])
    return result


def _position_unrealized_pnl(pos: dict[str, Any]) -> float:
    try:
        return float(pos.get("unrealized_pnl"))
    except Exception:
        pass
    net_return = _position_net_return(pos)
    try:
        return float(pos.get("risk_budget") or 0.0) * net_return
    except Exception:
        return 0.0


def _position_net_return(pos: dict[str, Any]) -> float:
    try:
        return float(pos.get("net_return"))
    except Exception:
        pass
    try:
        entry = float(pos.get("entry_price"))
        mark = float(pos.get("mark_price"))
    except Exception:
        return 0.0
    if entry <= 0 or mark <= 0:
        return 0.0
    raw = mark / entry - 1.0
    gross = raw if pos.get("side") == "long" else -raw
    return gross - 0.0014


def start_c_auto_v2_paper(environment: str, fresh_start: bool = False) -> dict[str, Any]:
    existing = find_c_auto_v2_paper_processes(C_AUTO_V2_STATE_ID)
    archived_session = None
    if existing:
        live_existing = [proc for proc in existing if proc.get("source_mode") == "live"]
        stale_existing = [proc for proc in existing if proc.get("source_mode") != "live"]
        if live_existing and not fresh_start:
            return {"ok": True, "already_running": True, "processes": live_existing, "status": c_auto_v2_paper_status(environment=environment)}
        for proc in live_existing + stale_existing:
            state_id = str(proc.get("state_id") or C_AUTO_V2_STATE_ID)
            proc_env = str(proc.get("environment") or environment)
            stop_path = CONTROL_DIR / f"c_auto_v2_paper_{state_id}_{proc_env}.stop"
            stop_path.write_text(time.strftime("%Y-%m-%dT%H:%M:%S%z"))
            try:
                os.kill(int(proc["pid"]), 15)
            except OSError:
                pass
        time.sleep(0.5)
    if fresh_start:
        archived_session = archive_c_auto_v2_paper_session(C_AUTO_V2_STATE_ID, environment, "fresh_launcher_start")
    result = run_script(
        [
            "python3",
            "scripts/run_c_auto_v2_paper.py",
            "--source-mode",
            "live",
            "--state-id",
            "fixed1000_conservative",
            "--environment",
            environment,
            "--initial-capital",
            f"{C_AUTO_V2_CAPITAL_USDT:g}",
            "--fixed-notional-capital",
            f"{C_AUTO_V2_CAPITAL_USDT:g}",
            "--dataset-id",
            "c_auto_feature_store_rebuild_161_ohlcv_snapshot_v1",
            "--quality-id",
            "c_auto_dataset_quality_rebuild_161_ohlcv_v1",
            "--deriv-run-id",
            "c_auto_live_derivatives_5m",
            "--snapshot-run-id",
            "rebuild_161_market_snapshot_20260508",
            "--max-symbols",
            "80",
            "--refresh-max-symbols",
            "150",
            "--lookback-days",
            "240",
            "--max-train-rows",
            "250000",
            "--max-market-age-sec",
            "7200",
            "--min-fresh-symbols",
            "40",
            "--loop",
            "--interval-sec",
            "300",
        ],
        f"c_auto_v2_paper_fixed1000_{environment}",
    )
    result.update(
        {
            "ok": True,
            "strategy": C_AUTO_V2_STRATEGY_ID,
            "environment": environment,
            "mode": "paper",
            "fresh_start": fresh_start,
            "archived_session": archived_session,
        }
    )
    return result


def start_c_auto_v2_micro_live(environment: str, confirm: bool = False) -> dict[str, Any]:
    if environment != "competition":
        raise ValueError("micro-live 只允许比赛账户环境")
    if not confirm:
        raise ValueError("micro-live requires confirm_real=true and confirm_competition=true")
    state_id = "micro_live_competition"
    existing = find_c_auto_v2_micro_live_processes(state_id)
    if existing:
        return {"ok": True, "already_running": True, "processes": existing, "status": c_auto_v2_micro_live_status(state_id, environment)}
    stop_path = CONTROL_DIR / f"c_auto_v2_micro_live_{state_id}_{environment}.stop"
    try:
        stop_path.unlink()
    except FileNotFoundError:
        pass
    result = run_script(
        [
            "python3",
            "scripts/run_c_auto_v2_micro_live.py",
            "--state-id",
            state_id,
            "--paper-state-id",
            C_AUTO_V2_STATE_ID,
            "--environment",
            environment,
            "--initial-capital",
            f"{C_AUTO_V2_CAPITAL_USDT:g}",
            "--fixed-notional-capital",
            f"{C_AUTO_V2_CAPITAL_USDT:g}",
            "--dataset-id",
            "c_auto_feature_store_rebuild_161_ohlcv_snapshot_v1",
            "--quality-id",
            "c_auto_dataset_quality_rebuild_161_ohlcv_v1",
            "--deriv-run-id",
            "c_auto_live_derivatives_5m",
            "--snapshot-run-id",
            "rebuild_161_market_snapshot_20260508",
            "--max-symbols",
            "80",
            "--refresh-max-symbols",
            "0",
            "--max-market-age-sec",
            "7200",
            "--min-fresh-symbols",
            "40",
            "--daily-budget-usdt",
            "50",
            "--per-symbol-margin-usdt",
            "10",
            "--first-48h-max-positions",
            "2",
            "--steady-state-max-positions",
            "5",
            "--default-leverage",
            "2",
            "--max-leverage",
            "2",
            "--interval-sec",
            "300",
            "--entry-scan-minutes",
            "15",
            "--run-on-start-entry",
            "--confirm-micro-live",
        ],
        f"c_auto_v2_micro_live_{environment}",
    )
    result.update({"ok": True, "strategy": C_AUTO_V2_STRATEGY_ID, "environment": environment, "mode": "real", "source_mode": "micro_live"})
    return result


def archive_c_auto_v2_paper_session(state_id: str, environment: str, reason: str) -> dict[str, Any] | None:
    prefix = f"{state_id}_{environment}"
    active_paths = [
        C_AUTO_V2_PAPER_DIR / f"{prefix}.json",
        C_AUTO_V2_PAPER_DIR / f"{prefix}_scheduler.json",
        C_AUTO_V2_PAPER_DIR / f"{prefix}_equity.jsonl",
        C_AUTO_V2_PAPER_DIR / f"{prefix}_ledger.jsonl",
    ]
    existing_paths = [path for path in active_paths if path.exists()]
    if not existing_paths:
        return None
    stamp = time.strftime("%Y%m%d_%H%M%S")
    session_id = f"{prefix}_{stamp}"
    archive_dir = C_AUTO_V2_PAPER_DIR / "archive" / session_id
    archive_dir.mkdir(parents=True, exist_ok=True)
    archived_files: list[str] = []
    for path in existing_paths:
        target = archive_dir / path.name
        shutil.move(str(path), str(target))
        archived_files.append(str(target.relative_to(ROOT_DIR)))
    manifest = {
        "session_id": session_id,
        "state_id": state_id,
        "environment": environment,
        "reason": reason,
        "archived_at": datetime.now(timezone.utc).isoformat(),
        "files": archived_files,
    }
    manifest_path = archive_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return {
        "session_id": session_id,
        "archive_dir": str(archive_dir.relative_to(ROOT_DIR)),
        "files": archived_files,
        "manifest": str(manifest_path.relative_to(ROOT_DIR)),
    }


def stop_pro_paper() -> dict[str, Any]:
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    stopped: list[int] = []
    stop_files: list[str] = []
    for proc in find_pro_paper_processes():
        strategy_id = proc.get("strategy_id") or "unknown"
        environment = proc.get("environment") or "personal"
        stop_path = CONTROL_DIR / f"pro_paper_{strategy_id}_{environment}.stop"
        stop_path.write_text(time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        stop_files.append(str(stop_path.relative_to(ROOT_DIR)))
        try:
            os.kill(int(proc["pid"]), 15)
            stopped.append(int(proc["pid"]))
        except OSError:
            continue
    return {"ok": True, "stopped_pids": stopped, "stop_files": stop_files}


def stop_c_auto_v2_paper() -> dict[str, Any]:
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    stopped: list[int] = []
    stop_files: list[str] = []
    flattened: list[str] = []
    for proc in find_c_auto_v2_paper_processes():
        state_id = proc.get("state_id") or "fixed1000_conservative"
        environment = proc.get("environment") or "personal"
        stop_path = CONTROL_DIR / f"c_auto_v2_paper_{state_id}_{environment}.stop"
        stop_path.write_text(time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        stop_files.append(str(stop_path.relative_to(ROOT_DIR)))
        flattened_path = flatten_c_auto_v2_paper_state(state_id, environment, "launcher_stop")
        if flattened_path:
            flattened.append(flattened_path)
        try:
            os.kill(int(proc["pid"]), 15)
            stopped.append(int(proc["pid"]))
        except OSError:
            continue
    return {"ok": True, "stopped_pids": stopped, "stop_files": stop_files, "flattened_state_files": flattened}


def stop_c_auto_v2_micro_live() -> dict[str, Any]:
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    stopped: list[int] = []
    stop_files: list[str] = []
    flattened: list[dict[str, Any]] = []
    for proc in find_c_auto_v2_micro_live_processes():
        state_id = proc.get("state_id") or "micro_live_competition"
        environment = proc.get("environment") or "competition"
        stop_path = CONTROL_DIR / f"c_auto_v2_micro_live_{state_id}_{environment}.stop"
        stop_path.write_text(time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        stop_files.append(str(stop_path.relative_to(ROOT_DIR)))
        flattened.append(flatten_c_auto_v2_micro_live_state(str(state_id), str(environment), "launcher_stop"))
        try:
            os.kill(int(proc["pid"]), 15)
            stopped.append(int(proc["pid"]))
        except OSError:
            continue
    return {"ok": True, "stopped_pids": stopped, "stop_files": stop_files, "flattened": flattened}


def flatten_c_auto_v2_micro_live_state(state_id: str, environment: str, reason: str) -> dict[str, Any]:
    state_path = C_AUTO_V2_MICRO_LIVE_DIR / f"{state_id}_{environment}.json"
    state = read_json(state_path)
    if not isinstance(state, dict):
        return {"ok": False, "reason": "missing_state"}
    positions = dict(state.get("positions") or {})
    events: list[dict[str, Any]] = []
    for symbol, pos in positions.items():
        inst_id = str(pos.get("inst_id") or _symbol_to_swap_inst_id(symbol))
        close_result = _run_okx_json(["okx", "--profile", "live", "--json", "swap", "close", "--instId", inst_id, "--mgnMode", "isolated", "--posSide", "net", "--autoCxl"])
        events.append(
            {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "event": "forced_exit",
                "symbol": symbol,
                "side": pos.get("side"),
                "reason": reason,
                "close": close_result,
            }
        )
    state["positions"] = {}
    state["open_risk"] = 0.0
    state["unrealized_pnl"] = 0.0
    state["runner_status"] = "stopped_flat"
    state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    state["ledger_tail"] = (list(state.get("ledger_tail", [])) + events)[-80:]
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return {"ok": True, "positions_closed": len(positions), "events": events}


def flatten_c_auto_v2_paper_state(state_id: str, environment: str, reason: str) -> str | None:
    state_path = C_AUTO_V2_PAPER_DIR / f"{state_id}_{environment}.json"
    state = read_json(state_path)
    if not isinstance(state, dict):
        return None
    positions = dict(state.get("positions") or {})
    if not positions:
        state["positions"] = {}
        state["open_risk"] = 0.0
        state["unrealized_pnl"] = 0.0
    else:
        now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        forced_events = []
        for symbol, pos in positions.items():
            forced_events.append(
                {
                    "event": "forced_stop_flatten",
                    "symbol": symbol,
                    "side": pos.get("side"),
                    "reason": reason,
                    "ts": now,
                    "pnl": 0.0,
                    "net_return": 0.0,
                }
            )
        ledger_tail = list(state.get("ledger_tail", []))
        state["ledger_tail"] = (ledger_tail + forced_events)[-40:]
        state["positions"] = {}
        state["open_risk"] = 0.0
        state["unrealized_pnl"] = 0.0
    realized_nav = float(state.get("realized_nav") or state.get("nav") or state.get("cash") or 1000.0)
    state["nav"] = realized_nav
    state["cash"] = realized_nav
    metrics = dict(state.get("metrics") or {})
    metrics["current_nav"] = realized_nav
    state["metrics"] = metrics
    state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    state["runner_status"] = "stopped_flat"
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    return str(state_path.relative_to(ROOT_DIR))


def iter_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    try:
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return records


def find_download_processes(run_id: str | None = None) -> list[dict[str, Any]]:
    try:
        proc = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            cwd=str(ROOT_DIR),
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []

    matches: list[dict[str, Any]] = []
    self_pid = os.getpid()
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            pid_raw, command = stripped.split(None, 1)
            pid = int(pid_raw)
        except ValueError:
            continue
        if pid == self_pid:
            continue
        known_download = (
            "scripts/fetch_training_history.py" in command
            or "scripts/fetch_derivatives_structure.py" in command
        )
        if not known_download:
            continue
        if run_id and run_id not in command:
            continue
        matches.append({"pid": pid, "command": command, "run_id": _run_id_from_command(command)})
    return matches


def find_data_refresh_processes() -> list[dict[str, Any]]:
    try:
        proc = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            cwd=str(ROOT_DIR),
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    matches: list[dict[str, Any]] = []
    self_pid = os.getpid()
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if not stripped or "engine/data/refresh_scheduler.py" not in stripped:
            continue
        try:
            pid_raw, command = stripped.split(None, 1)
            pid = int(pid_raw)
        except ValueError:
            continue
        if pid == self_pid:
            continue
        matches.append({"pid": pid, "command": command})
    return matches


def find_smartmoney_diffusion_processes() -> list[dict[str, Any]]:
    try:
        proc = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            cwd=str(ROOT_DIR),
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    matches: list[dict[str, Any]] = []
    self_pid = os.getpid()
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if not stripped or "scripts/run_smartmoney_diffusion_collector.py" not in stripped:
            continue
        try:
            pid_raw, command = stripped.split(None, 1)
            pid = int(pid_raw)
        except ValueError:
            continue
        if pid == self_pid:
            continue
        matches.append({"pid": pid, "command": command})
    return matches


def data_refresh_status() -> dict[str, Any]:
    status_path = DATA_REFRESH_DIR / "status.json"
    progress_path = DATA_REFRESH_DIR / "progress.jsonl"
    status = read_json(status_path) or {}
    processes = find_data_refresh_processes()
    if not status and not processes:
        return {"available": False, "running": False, "message": "no data refresh scheduler found"}
    return {
        "available": True,
        "running": bool(processes),
        "processes": processes,
        "status": status,
        "progress_tail": iter_jsonl(progress_path)[-20:],
        "status_path": str(status_path.relative_to(ROOT_DIR)) if status_path.exists() else None,
    }


def smartmoney_diffusion_status() -> dict[str, Any]:
    status_path = SMARTMONEY_DIFFUSION_DIR / "collector_status.json"
    status = read_json(status_path) or {}
    processes = find_smartmoney_diffusion_processes()
    if not status and not processes:
        return {"available": False, "running": False, "message": "no smartmoney diffusion collector found"}
    return {
        "available": True,
        "running": bool(processes),
        "processes": processes,
        "status": status,
        "status_path": str(status_path.relative_to(ROOT_DIR)) if status_path.exists() else None,
    }


def c_auto_daily_review_status() -> dict[str, Any]:
    status_path = C_AUTO_V2_PAPER_DIR / "daily_review_scheduler.json"
    status = read_json(status_path) or {}
    processes = find_c_auto_daily_review_processes()
    if not status and not processes:
        return {"available": False, "running": False, "message": "no daily review scheduler found"}
    return {
        "available": True,
        "running": bool(processes),
        "processes": processes,
        "status": status,
        "status_path": str(status_path.relative_to(ROOT_DIR)) if status_path.exists() else None,
    }


def start_c_auto_daily_review(environment: str) -> dict[str, Any]:
    existing = find_c_auto_daily_review_processes()
    live_existing = [proc for proc in existing if (proc.get("environment") or environment) == environment]
    if live_existing:
        return {"ok": True, "already_running": True, "processes": live_existing, "status": c_auto_daily_review_status()}
    result = run_script(
        [
            "python3",
            "scripts/run_c_auto_daily_review_scheduler.py",
            "--state-id",
            C_AUTO_V2_STATE_ID,
            "--environment",
            environment,
            "--time",
            "23:58",
            "--run-on-start",
        ],
        f"c_auto_daily_review_{environment}",
    )
    result.update({"ok": True, "service": "c_auto_daily_review", "environment": environment})
    return result


def stop_c_auto_daily_review() -> dict[str, Any]:
    stopped: list[int] = []
    for proc in find_c_auto_daily_review_processes():
        try:
            os.kill(int(proc["pid"]), 15)
            stopped.append(int(proc["pid"]))
        except OSError:
            continue
    return {"ok": True, "stopped_pids": stopped}


def start_data_refresh() -> dict[str, Any]:
    existing = find_data_refresh_processes()
    if existing:
        return {"ok": True, "already_running": True, "processes": existing, "status": data_refresh_status()}
    result = run_script(
        [
            "python3",
            "engine/data/refresh_scheduler.py",
            "--interval-sec",
            "900",
            "--max-symbols",
            "150",
            "--timeframes",
            "5m,15m,1h,4h,1d",
            "--lookback-days",
            "3",
            "--sleep-sec",
            "0.2",
            "--derivatives-max-symbols",
            "150",
            "--derivatives-run-id",
            "c_auto_live_derivatives_5m",
            "--derivatives-kinds",
            "funding,open_interest,long_short",
            "--derivatives-timeframe",
            "5m",
            "--derivatives-lookback-days",
            "3",
        ],
        "data_refresh",
    )
    result.update({"ok": True, "service": "data_refresh"})
    return result


def start_smartmoney_diffusion() -> dict[str, Any]:
    existing = find_smartmoney_diffusion_processes()
    if existing:
        return {"ok": True, "already_running": True, "processes": existing, "status": smartmoney_diffusion_status()}
    result = run_script(
        [
            "python3",
            "scripts/run_smartmoney_diffusion_collector.py",
            "--symbols",
            "auto",
            "--max-symbols",
            "80",
            "--limit",
            "72",
            "--period",
            "7",
            "--lmt-num",
            "100",
            "--interval-sec",
            "3600",
        ],
        "smartmoney_diffusion",
    )
    result.update({"ok": True, "service": "smartmoney_diffusion"})
    return result


def stop_data_refresh() -> dict[str, Any]:
    stopped: list[int] = []
    for proc in find_data_refresh_processes():
        try:
            os.kill(int(proc["pid"]), 15)
            stopped.append(int(proc["pid"]))
        except OSError:
            continue
    return {"ok": True, "stopped_pids": stopped}


def stop_smartmoney_diffusion() -> dict[str, Any]:
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    stopped: list[int] = []
    stop_path = CONTROL_DIR / "smartmoney_diffusion_collector.stop"
    stop_path.write_text(time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    for proc in find_smartmoney_diffusion_processes():
        try:
            os.kill(int(proc["pid"]), 15)
            stopped.append(int(proc["pid"]))
        except OSError:
            continue
    return {"ok": True, "stopped_pids": stopped, "stop_file": str(stop_path.relative_to(ROOT_DIR))}


def activate_kill_switch(reason: str = "launcher stop all") -> dict[str, Any]:
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "active": True,
        "reason": reason,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    KILL_SWITCH_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    return {"active": True, "path": str(KILL_SWITCH_PATH.relative_to(ROOT_DIR)), "reason": reason}


def clear_launcher_kill_switch() -> dict[str, Any]:
    if not KILL_SWITCH_PATH.exists():
        return {"active": False, "cleared": False}
    current = read_json(KILL_SWITCH_PATH) or {}
    reason = str(current.get("reason") or "")
    if reason != "launcher pause all":
        return {"active": True, "cleared": False, "reason": reason}
    KILL_SWITCH_PATH.unlink()
    return {"active": False, "cleared": True, "reason": reason}


def kill_switch_status() -> dict[str, Any]:
    if not KILL_SWITCH_PATH.exists():
        return {"active": False}
    current = read_json(KILL_SWITCH_PATH) or {}
    return {
        "active": True,
        "path": str(KILL_SWITCH_PATH.relative_to(ROOT_DIR)),
        "reason": str(current.get("reason") or KILL_SWITCH_PATH.read_text().strip()),
        "created_at": current.get("created_at"),
    }


def cancel_all_open_swap_orders() -> dict[str, Any]:
    profiles = _okx_cancel_profiles()
    results = [_cancel_profile_open_swap_orders(profile) for profile in profiles]
    return {
        "ok": True,
        "profiles": results,
        "orders_found": sum(int(item.get("orders_found", 0) or 0) for item in results),
        "orders_cancelled": sum(int(item.get("orders_cancelled", 0) or 0) for item in results),
        "orders_failed": sum(int(item.get("orders_failed", 0) or 0) for item in results),
    }


def _okx_cancel_profiles() -> list[str]:
    configured: list[str] = []
    try:
        from config.settings import get_okx_profiles

        profiles = get_okx_profiles()
        configured = [str(name) for name in profiles.keys()]
    except Exception:
        configured = []

    preferred = ["demo", "live", "personal"]
    merged = preferred + configured
    out: list[str] = []
    for profile in merged:
        if profile and profile not in out:
            out.append(profile)
    return out


def _cancel_profile_open_swap_orders(profile: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "profile": profile,
        "orders_found": 0,
        "orders_cancelled": 0,
        "orders_failed": 0,
        "errors": [],
        "cancelled": [],
    }
    orders = _list_open_swap_orders(profile)
    if orders is None:
        result["errors"].append("unable to list open orders")
        return result
    result["orders_found"] = len(orders)
    for order in orders:
        inst_id = str(order.get("instId") or order.get("inst_id") or "").strip()
        ord_id = str(order.get("ordId") or order.get("ord_id") or order.get("orderId") or "").strip()
        if not inst_id or not ord_id:
            result["orders_failed"] += 1
            result["errors"].append(f"missing instId/ordId in order: {order}")
            continue
        cancel = _run_okx_json(["okx", "--profile", profile, "swap", "cancel", inst_id, "--ordId", ord_id])
        if cancel["returncode"] == 0:
            result["orders_cancelled"] += 1
            result["cancelled"].append({"instId": inst_id, "ordId": ord_id})
        else:
            result["orders_failed"] += 1
            result["errors"].append(cancel["message"])
    return result


def _list_open_swap_orders(profile: str) -> list[dict[str, Any]] | None:
    direct = _run_okx_json(["okx", "--profile", profile, "--json", "swap", "orders", "--status", "open"])
    if direct["returncode"] == 0:
        return _as_order_list(direct["data"])

    orders: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    had_success = False
    candidates = _candidate_swap_inst_ids(profile)[:20]
    for inst_id in candidates:
        resp = _run_okx_json(["okx", "--profile", profile, "--json", "swap", "orders", "--instId", inst_id, "--status", "open"])
        if resp["returncode"] != 0:
            continue
        had_success = True
        for order in _as_order_list(resp["data"]):
            ord_id = str(order.get("ordId") or order.get("ord_id") or order.get("orderId") or "")
            key = (str(order.get("instId") or inst_id), ord_id)
            if ord_id and key not in seen:
                seen.add(key)
                if not order.get("instId"):
                    order["instId"] = inst_id
                orders.append(order)
    if orders:
        return orders
    return [] if had_success else None


def _candidate_swap_inst_ids(profile: str) -> list[str]:
    inst_ids: list[str] = []
    positions = _run_okx_json(["okx", "--profile", profile, "--json", "account", "positions"])
    if positions["returncode"] == 0:
        for item in _as_order_list(positions["data"]):
            inst_id = str(item.get("instId") or item.get("inst_id") or "").strip()
            if inst_id:
                inst_ids.append(inst_id)

    try:
        catalog = read_json(ENGINE_DIR / "data" / "catalog.json") or {}
        for dataset in catalog.get("datasets", []):
            for symbol in dataset.get("symbols", []):
                inst_ids.append(_symbol_to_swap_inst_id(str(symbol)))
    except Exception:
        pass

    cache_dir = ENGINE_DIR / "data" / "cache"
    if cache_dir.exists():
        for path in cache_dir.glob("*_USDT_futures_*"):
            parts = path.name.split("_USDT_futures_", 1)
            if parts and parts[0]:
                inst_ids.append(f"{parts[0]}-USDT-SWAP")

    out: list[str] = []
    for inst_id in inst_ids:
        clean = inst_id.strip()
        if clean and clean not in out:
            out.append(clean)
    return out


def _symbol_to_swap_inst_id(symbol: str) -> str:
    if symbol.endswith("-USDT-SWAP"):
        return symbol
    base = symbol.split("/", 1)[0].replace("_", "-").strip()
    return f"{base}-USDT-SWAP"


def _run_okx_json(cmd: list[str]) -> dict[str, Any]:
    try:
        proc = subprocess.run(cmd, cwd=str(ROOT_DIR), capture_output=True, text=True, timeout=20)
    except Exception as exc:
        return {"returncode": 1, "data": None, "message": f"{cmd[:4]} failed: {exc}"}
    stdout = proc.stdout.strip()
    stderr = proc.stderr.strip()
    data = None
    if stdout:
        try:
            data = json.loads(stdout)
        except Exception:
            data = None
    return {
        "returncode": proc.returncode,
        "data": data,
        "message": (stderr or stdout or f"exit={proc.returncode}")[:500],
    }


def _as_order_list(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("data", "orders", "result"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return [data]
    return []


def _run_id_from_command(command: str) -> str | None:
    parts = command.split()
    for i, part in enumerate(parts):
        if part == "--run-id" and i + 1 < len(parts):
            return parts[i + 1]
        if part.startswith("--run-id="):
            return part.split("=", 1)[1]
    return None


def _download_roots() -> list[Path]:
    return [TRAINING_HISTORY_DIR, DERIVATIVES_STRUCTURE_DIR]


def _download_run_dir(run_id: str) -> Path | None:
    for root in _download_roots():
        candidate = root / run_id
        if (candidate / "manifest.json").exists():
            return candidate
    return None


def latest_download_run_id() -> str | None:
    active = [p.get("run_id") for p in find_download_processes(None) if p.get("run_id")]
    if active:
        return str(active[0])
    candidates = []
    for root in _download_roots():
        if root.exists():
            candidates.extend([p for p in root.iterdir() if p.is_dir() and (p / "manifest.json").exists()])
    if candidates:
        latest = max(candidates, key=lambda p: (p / "manifest.json").stat().st_mtime)
        return latest.name
    preferred_deriv = DERIVATIVES_STRUCTURE_DIR / DEFAULT_DERIVATIVES_RUN_ID
    if preferred_deriv.exists():
        return DEFAULT_DERIVATIVES_RUN_ID
    preferred = TRAINING_HISTORY_DIR / DEFAULT_DOWNLOAD_RUN_ID
    if preferred.exists():
        return DEFAULT_DOWNLOAD_RUN_ID
    return None


def latest_monster_watchlist_id() -> str | None:
    if not MONSTER_EVENTS_DIR.exists():
        return None
    candidates = [
        p
        for p in MONSTER_EVENTS_DIR.iterdir()
        if p.is_dir() and (p / "watchlist.parquet").exists() and (p / "manifest.json").exists()
    ]
    if candidates:
        latest = max(candidates, key=lambda p: (p / "manifest.json").stat().st_mtime)
        return latest.name
    preferred = MONSTER_EVENTS_DIR / DEFAULT_MONSTER_WATCHLIST_ID
    if (preferred / "manifest.json").exists():
        return DEFAULT_MONSTER_WATCHLIST_ID
    if not candidates:
        return None


def latest_monster_orderbook_id() -> str | None:
    if not MONSTER_EVENTS_DIR.exists():
        return None
    candidates = [
        p
        for p in MONSTER_EVENTS_DIR.iterdir()
        if p.is_dir() and p.name.startswith("monster_orderbook_") and (p / "manifest.json").exists()
    ]
    if not candidates:
        return None
    latest = max(candidates, key=lambda p: (p / "manifest.json").stat().st_mtime)
    return latest.name


def latest_monster_derivatives_id() -> str | None:
    if not MONSTER_EVENTS_DIR.exists():
        return None
    candidates = [
        p
        for p in MONSTER_EVENTS_DIR.iterdir()
        if p.is_dir() and p.name.startswith("monster_derivatives_") and (p / "manifest.json").exists()
    ]
    if not candidates:
        return None
    latest = max(candidates, key=lambda p: (p / "manifest.json").stat().st_mtime)
    return latest.name


def latest_monster_auto_refresh_id() -> str | None:
    if not MONSTER_EVENTS_DIR.exists():
        return None
    candidates = [
        p
        for p in MONSTER_EVENTS_DIR.iterdir()
        if p.is_dir() and p.name.startswith("monster_auto_refresh_") and ((p / "manifest.json").exists() or (p / "status.json").exists())
    ]
    if not candidates:
        return None
    latest = max(candidates, key=lambda p: max((p / "manifest.json").stat().st_mtime if (p / "manifest.json").exists() else 0, (p / "status.json").stat().st_mtime if (p / "status.json").exists() else 0))
    return latest.name


def find_monster_processes() -> list[dict[str, Any]]:
    try:
        proc = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            cwd=str(ROOT_DIR),
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []

    matches: list[dict[str, Any]] = []
    self_pid = os.getpid()
    needles = (
        "scripts/run_monster_refresh_and_score.py",
        "scripts/run_monster_paper.py",
        "scripts/collect_monster_orderbook.py",
        "scripts/collect_monster_derivatives.py",
        "scripts/run_monster_auto_refresh.py",
    )
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            pid_raw, command = stripped.split(None, 1)
            pid = int(pid_raw)
        except ValueError:
            continue
        if pid == self_pid or not any(needle in command for needle in needles):
            continue
        matches.append({"pid": pid, "command": command})
    return matches


def monster_status(run_id: str | None = None) -> dict[str, Any]:
    selected = run_id or latest_monster_watchlist_id()
    if not selected:
        return {"ok": True, "available": False, "message": "no monster watchlist found"}
    run_dir = MONSTER_EVENTS_DIR / selected
    manifest = read_json(run_dir / "manifest.json") or {}
    top = read_json(run_dir / "watchlist_top.json") or []
    if not isinstance(top, list):
        top = []
    trade_candidates = [item for item in top if int(item.get("trade_candidate_flag") or 0) == 1]
    fresh = sum(1 for item in top if int(item.get("fresh_data_flag") or 0) == 1)
    liquidity = sum(1 for item in top if int(item.get("liquidity_gate") or 0) == 1)
    return {
        "ok": True,
        "available": True,
        "run_id": selected,
        "run_dir": str(run_dir.relative_to(ROOT_DIR)),
        "manifest": manifest,
        "top": top[:30],
        "trade_candidates": trade_candidates[:12],
        "top_count": len(top),
        "fresh_top_count": fresh,
        "liquidity_top_count": liquidity,
        "trade_candidate_count": len(trade_candidates),
        "updated_at": manifest.get("created_at"),
        "processes": find_monster_processes(),
        "orderbook": monster_orderbook_status(),
        "derivatives": monster_derivatives_status(),
        "paper": monster_paper_status(),
        "auto_refresh": monster_auto_refresh_status(),
    }


def refresh_monster() -> dict[str, Any]:
    existing = find_monster_processes()
    if existing:
        return {"ok": True, "already_running": True, "processes": existing}
    result = run_script(["python3", "scripts/run_monster_refresh_and_score.py"], "monster_refresh")
    result.update({"ok": True})
    return result


def monster_orderbook_status(run_id: str | None = None) -> dict[str, Any]:
    selected = run_id or latest_monster_orderbook_id()
    if not selected:
        return {"available": False, "message": "no monster orderbook run found"}
    run_dir = MONSTER_EVENTS_DIR / selected
    manifest = read_json(run_dir / "manifest.json") or {}
    status = read_json(run_dir / "status.json") or {}
    progress = iter_jsonl(run_dir / "progress.jsonl")
    processes = [p for p in find_monster_processes() if "collect_monster_orderbook.py" in p.get("command", "")]
    ok_count = sum(1 for item in progress if item.get("status") == "ok")
    failed_count = sum(1 for item in progress if item.get("status") == "failed")
    return {
        "available": True,
        "run_id": selected,
        "run_dir": str(run_dir.relative_to(ROOT_DIR)),
        "manifest": manifest,
        "status": status,
        "running": bool(processes),
        "processes": processes,
        "ok": ok_count,
        "failed": failed_count,
        "last_record": progress[-1] if progress else None,
        "updated_at": status.get("updated_at") or manifest.get("completed_at") or manifest.get("created_at"),
    }


def monster_derivatives_status(run_id: str | None = None) -> dict[str, Any]:
    selected = run_id or latest_monster_derivatives_id()
    if not selected:
        return {"available": False, "message": "no monster derivatives run found"}
    run_dir = MONSTER_EVENTS_DIR / selected
    manifest = read_json(run_dir / "manifest.json") or {}
    status = read_json(run_dir / "status.json") or {}
    progress = iter_jsonl(run_dir / "progress.jsonl")
    processes = [p for p in find_monster_processes() if "collect_monster_derivatives.py" in p.get("command", "")]
    ok_count = sum(1 for item in progress if item.get("status") == "ok")
    failed_count = sum(1 for item in progress if item.get("status") == "failed")
    return {
        "available": True,
        "run_id": selected,
        "run_dir": str(run_dir.relative_to(ROOT_DIR)),
        "manifest": manifest,
        "status": status,
        "running": bool(processes),
        "processes": processes,
        "ok": ok_count,
        "failed": failed_count,
        "last_record": progress[-1] if progress else None,
        "updated_at": status.get("updated_at") or manifest.get("completed_at") or manifest.get("created_at"),
    }


def start_monster_orderbook() -> dict[str, Any]:
    existing = [p for p in find_monster_processes() if "collect_monster_orderbook.py" in p.get("command", "")]
    if existing:
        return {"ok": True, "already_running": True, "processes": existing}
    dataset_id = f"monster_orderbook_live_{time.strftime('%Y%m%d_%H%M%S')}"
    result = run_script(
        [
            "python3",
            "scripts/collect_monster_orderbook.py",
            "--dataset-id",
            dataset_id,
            "--candidate-only",
            "--top-n",
            "20",
            "--samples",
            "0",
            "--limit",
            "20",
            "--interval-sec",
            "10",
            "--sleep-sec",
            "0.15",
        ],
        f"monster_orderbook_{dataset_id}",
    )
    result.update({"ok": True, "dataset_id": dataset_id})
    return result


def start_monster_derivatives() -> dict[str, Any]:
    existing = [p for p in find_monster_processes() if "collect_monster_derivatives.py" in p.get("command", "")]
    if existing:
        return {"ok": True, "already_running": True, "processes": existing}
    dataset_id = f"monster_derivatives_live_{time.strftime('%Y%m%d_%H%M%S')}"
    result = run_script(
        [
            "python3",
            "scripts/collect_monster_derivatives.py",
            "--dataset-id",
            dataset_id,
            "--candidate-only",
            "--top-n",
            "20",
            "--samples",
            "0",
            "--timeframe",
            "5m",
            "--interval-sec",
            "60",
            "--sleep-sec",
            "0.2",
        ],
        f"monster_derivatives_{dataset_id}",
    )
    result.update({"ok": True, "dataset_id": dataset_id})
    return result


def monster_paper_status(state_id: str = "lottery_live") -> dict[str, Any]:
    state_path = MONSTER_PAPER_DIR / f"{state_id}.json"
    ledger_path = MONSTER_PAPER_DIR / f"{state_id}_ledger.jsonl"
    equity_path = MONSTER_PAPER_DIR / f"{state_id}_equity.jsonl"
    state = read_json(state_path) or {}
    ledger = iter_jsonl(ledger_path)
    equity = iter_jsonl(equity_path)
    processes = [p for p in find_monster_processes() if "run_monster_paper.py" in p.get("command", "")]
    if not state and not ledger and not equity and not processes:
        return {"available": False, "state_id": state_id, "message": "no monster paper state found"}
    positions = state.get("positions") or {}
    realized_pnl = sum(float(item.get("pnl", 0.0) or 0.0) for item in ledger if item.get("event") in {"exit", "partial_exit"})
    metrics = _paper_metrics(equity, state, realized_pnl)
    return {
        "available": True,
        "state_id": state_id,
        "state_path": str(state_path.relative_to(ROOT_DIR)) if state_path.exists() else None,
        "ledger_path": str(ledger_path.relative_to(ROOT_DIR)) if ledger_path.exists() else None,
        "equity_path": str(equity_path.relative_to(ROOT_DIR)) if equity_path.exists() else None,
        "running": bool(processes),
        "processes": processes,
        "updated_at": state.get("updated_at"),
        "cash": state.get("cash"),
        "nav": state.get("nav"),
        "unrealized_pnl": state.get("unrealized_pnl"),
        "realized_pnl": realized_pnl,
        "open_risk": sum(float(pos.get("risk_budget", 0.0)) for pos in positions.values()) if isinstance(positions, dict) else 0.0,
        "positions": positions,
        "live_gates_enabled": state.get("live_gates_enabled"),
        "live_gate_pass_count": state.get("live_gate_pass_count"),
        "metrics": metrics,
        "equity": equity[-240:],
        "ledger_tail": ledger[-20:],
    }


def _paper_metrics(equity: list[dict[str, Any]], state: dict[str, Any], realized_pnl: float) -> dict[str, Any]:
    navs = [float(row["nav"]) for row in equity if row.get("nav") is not None]
    if not navs and state.get("nav") is not None:
        navs = [float(state["nav"])]
    if not navs:
        return {"realized_pnl": realized_pnl}
    initial_nav = navs[0]
    current_nav = navs[-1]
    peak = navs[0]
    max_dd = 0.0
    for nav in navs:
        peak = max(peak, nav)
        if peak > 0:
            max_dd = min(max_dd, nav / peak - 1.0)
    return {
        "initial_nav": initial_nav,
        "current_nav": current_nav,
        "total_return": current_nav / initial_nav - 1.0 if initial_nav else 0.0,
        "max_drawdown": max_dd,
        "realized_pnl": realized_pnl,
        "equity_points": len(equity),
    }


def start_monster_paper() -> dict[str, Any]:
    existing = [p for p in find_monster_processes() if "run_monster_paper.py" in p.get("command", "")]
    if existing:
        return {"ok": True, "already_running": True, "processes": existing, "status": monster_paper_status()}
    result = run_script(
        [
            "python3",
            "scripts/run_monster_paper.py",
            "--state-id",
            "lottery_live",
            "--mode",
            "lottery",
            "--initial-capital",
            "1000",
            "--risk-budget",
            "20",
            "--max-open-risk",
            "60",
            "--score-threshold",
            "0.85",
            "--quality-exit-min-hours",
            "4",
            "--exit-score-threshold",
            "0.80",
            "--exit-ret-1h-threshold",
            "-0.035",
            "--live-gate-exit-min-hours",
            "2",
            "--max-positions",
            "3",
            "--use-live-gates",
            "--loop",
            "--interval-sec",
            "300",
        ],
        "monster_paper_lottery_live",
    )
    result.update({"ok": True, "state_id": "lottery_live"})
    return result


def monster_auto_refresh_status(run_id: str | None = None) -> dict[str, Any]:
    selected = run_id or latest_monster_auto_refresh_id()
    processes = [p for p in find_monster_processes() if "run_monster_auto_refresh.py" in p.get("command", "")]
    if not selected:
        return {"available": False, "message": "no monster auto refresh run found", "running": bool(processes), "processes": processes}
    run_dir = MONSTER_EVENTS_DIR / selected
    manifest = read_json(run_dir / "manifest.json") or {}
    status = read_json(run_dir / "status.json") or {}
    progress = iter_jsonl(run_dir / "progress.jsonl")
    ok_count = sum(1 for item in progress if item.get("status") == "ok")
    failed_count = sum(1 for item in progress if item.get("status") == "failed")
    return {
        "available": True,
        "run_id": selected,
        "run_dir": str(run_dir.relative_to(ROOT_DIR)),
        "manifest": manifest,
        "status": status,
        "running": bool(processes),
        "processes": processes,
        "ok": ok_count,
        "failed": failed_count,
        "last_record": progress[-1] if progress else None,
        "updated_at": status.get("updated_at") or manifest.get("completed_at") or manifest.get("created_at"),
    }


def start_monster_auto_refresh() -> dict[str, Any]:
    existing = [p for p in find_monster_processes() if "run_monster_auto_refresh.py" in p.get("command", "")]
    if existing:
        return {"ok": True, "already_running": True, "processes": existing, "status": monster_auto_refresh_status()}
    run_id = f"monster_auto_refresh_{time.strftime('%Y%m%d_%H%M%S')}"
    result = run_script(
        [
            "python3",
            "scripts/run_monster_auto_refresh.py",
            "--run-id",
            run_id,
            "--interval-sec",
            "900",
            "--iterations",
            "0",
            "--run-prefix",
            "monster_auto",
            "--lookback-days",
            "3",
            "--sleep-sec",
            "0.25",
            "--min-quote-volume",
            "1000000",
            "--min-score",
            "0.75",
            "--max-ret-1h",
            "0.25",
            "--fresh-hours",
            "0.35",
        ],
        f"monster_auto_refresh_{run_id}",
    )
    result.update({"ok": True, "run_id": run_id})
    return result


def download_status(run_id: str | None = None) -> dict[str, Any]:
    selected_run_id = run_id or latest_download_run_id()
    if not selected_run_id:
        return {"ok": True, "available": False, "message": "no training history runs found"}

    run_dir = _download_run_dir(selected_run_id) or (TRAINING_HISTORY_DIR / selected_run_id)
    manifest = read_json(run_dir / "manifest.json") or {}
    heartbeat = read_json(run_dir / "status.json") or {}
    progress = iter_jsonl(run_dir / "progress.jsonl")
    timeframes = manifest.get("timeframes") or [manifest.get("timeframe") or "1h"]
    kinds = manifest.get("kinds") or ["ohlcv"]
    symbols = manifest.get("symbols") or []
    total_jobs = int(manifest.get("summary", {}).get("total_jobs") or (len(symbols) * len(timeframes) * len(kinds)))

    latest_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for record in progress:
        key = (str(record.get("symbol", "")), str(record.get("kind", "ohlcv")), str(record.get("timeframe", "")))
        latest_by_key[key] = record

    ok_count = sum(1 for r in latest_by_key.values() if r.get("status") == "ok")
    failed_count = sum(1 for r in latest_by_key.values() if r.get("status") == "failed")
    attempted_count = len(latest_by_key)
    last_record = progress[-1] if progress else None
    processes = find_download_processes(selected_run_id)

    current_symbol = heartbeat.get("current_symbol")
    current_kind = heartbeat.get("current_kind")
    next_symbol = None
    next_kind = None
    if processes:
        if not current_symbol and last_record:
            current_symbol = last_record.get("symbol")
            current_kind = last_record.get("kind")
        done_ok = {(k[0], k[1], k[2]) for k, r in latest_by_key.items() if r.get("status") == "ok"}
        for timeframe in timeframes:
            for symbol in symbols:
                for kind in kinds:
                    if (symbol, kind, timeframe) not in done_ok:
                        next_symbol = symbol
                        next_kind = kind
                        break
                if next_symbol:
                    break
            if next_symbol:
                break

    percent = round((ok_count / total_jobs) * 100, 2) if total_jobs else 0
    return {
        "ok": True,
        "available": True,
        "run_id": selected_run_id,
        "run_dir": str(run_dir.relative_to(ROOT_DIR)),
        "manifest": manifest,
        "heartbeat": heartbeat,
        "running": bool(processes),
        "processes": processes,
        "total_jobs": total_jobs,
        "downloaded": ok_count,
        "attempted": attempted_count,
        "failed_latest": failed_count,
        "remaining": max(total_jobs - ok_count, 0),
        "percent": percent,
        "current_symbol": current_symbol,
        "current_kind": current_kind,
        "next_symbol": next_symbol,
        "next_kind": next_kind,
        "last_record": last_record,
        "updated_at": last_record.get("ts") if last_record else manifest.get("created_at"),
        "progress_tail": progress[-20:],
    }


def resume_download(run_id: str | None = None) -> dict[str, Any]:
    selected_run_id = run_id or latest_download_run_id() or DEFAULT_DOWNLOAD_RUN_ID
    run_dir = _download_run_dir(selected_run_id) or (TRAINING_HISTORY_DIR / selected_run_id)
    manifest = read_json(run_dir / "manifest.json") or {}
    if not manifest:
        raise ValueError(f"download manifest not found for run_id={selected_run_id}")

    existing = find_download_processes(selected_run_id)
    if existing:
        return {"ok": True, "already_running": True, "processes": existing, "status": download_status(selected_run_id)}

    if manifest.get("download_type") == "derivatives_structure":
        cmd = [
            "python3",
            "scripts/fetch_derivatives_structure.py",
            "--run-id",
            selected_run_id,
            "--start",
            str(manifest.get("start", "2024-01-01")),
            "--end",
            str(manifest.get("end", "2026-04-24")),
            "--timeframe",
            str(manifest.get("timeframe", "5m")),
            "--kinds",
            ",".join(manifest.get("kinds") or ["funding", "open_interest", "long_short"]),
            "--limit",
            str(manifest.get("limit", 100)),
            "--sleep-sec",
            "1",
            "--retry-attempts",
            "4",
            "--retry-sleep-sec",
            "8",
        ]
        if manifest.get("source_manifest"):
            cmd.extend(["--symbols-manifest", str(ROOT_DIR / manifest["source_manifest"])])
    else:
        timeframes = ",".join(manifest.get("timeframes") or ["1h"])
        cmd = [
            "python3",
            "scripts/fetch_training_history.py",
            "--run-id",
            selected_run_id,
            "--start",
            str(manifest.get("start", "2024-01-01")),
            "--end",
            str(manifest.get("end", "2026-04-24")),
            "--timeframes",
            timeframes,
            "--sleep-sec",
            "4",
            "--retry-attempts",
            "8",
            "--retry-sleep-sec",
            "20",
            "--min-rows",
            "100",
        ]
        if manifest.get("source_manifest"):
            cmd.extend(["--symbols-manifest", str(ROOT_DIR / manifest["source_manifest"])])
        else:
            cmd.extend(["--min-volume-usd", str(manifest.get("min_volume_usd", 1_000_000)), "--max-symbols", str(manifest.get("max_symbols", 300))])
        if manifest.get("skip_funding"):
            cmd.append("--skip-funding")
    result = run_script(cmd, f"download_resume_{selected_run_id}")
    result.update({"ok": True, "run_id": selected_run_id})
    return result


def pause_download(run_id: str | None = None) -> dict[str, Any]:
    selected_run_id = run_id or latest_download_run_id()
    processes = find_download_processes(selected_run_id)
    stopped: list[int] = []
    for proc in processes:
        pid = int(proc["pid"])
        try:
            os.kill(pid, 15)
            stopped.append(pid)
        except OSError:
            continue
    return {"ok": True, "run_id": selected_run_id, "stopped_pids": stopped}


def normalize_port(value: Any) -> int:
    try:
        port = int(value)
    except Exception as exc:
        raise ValueError("dashboard port must be an integer") from exc
    if port < 1024 or port > 65535:
        raise ValueError("dashboard port must be between 1024 and 65535")
    return port


def run_script(args: list[str], prefix: str) -> dict[str, Any]:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    log_path = LOGS_DIR / f"launcher_{prefix}_{stamp}.log"
    log = log_path.open("a", buffering=1)
    cmd = list(args)
    if cmd and cmd[0] == "python3":
        cmd[0] = PYTHON_BIN
    log.write(f"$ {' '.join(args)}\n")
    env = os.environ.copy()
    user_site = site.getusersitepackages()
    pythonpath_parts = [user_site]
    existing_pythonpath = env.get("PYTHONPATH")
    if existing_pythonpath:
        pythonpath_parts.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(pythonpath_parts))
    env["OKX_TRADING_SYSTEM_PYTHON"] = PYTHON_BIN
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT_DIR),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        text=True,
    )
    return {
        "pid": proc.pid,
        "log_path": str(log_path.relative_to(ROOT_DIR)),
    }


class LauncherHandler(BaseHTTPRequestHandler):
    server_version = "OKXTradingLauncher/1.0"

    def send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(json_safe(payload), ensure_ascii=False, default=str, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            if path == "/api/health":
                self.send_json(200, {"ok": True, "service": "launcher", "pid": os.getpid()})
                return
            if path == "/api/status":
                self.send_json(200, self.status_payload())
                return
            if path == "/api/launch-options":
                self.send_json(
                    200,
                    {
                        "ok": True,
                        "strategies": strategy_options(),
                        "environments": [
                            {"id": "personal", "label": "个人"},
                            {"id": "demo", "label": "Demo"},
                            {"id": "competition", "label": "比赛"},
                        ],
                        "modes": [
                            {"id": "paper", "label": "纸面交易"},
                            {"id": "real", "label": "真实交易"},
                        ],
                        "primary_strategy_id": C_AUTO_V2_STRATEGY_ID,
                    },
                )
                return
            if path == "/api/pro-paper":
                self.send_json(200, {"ok": True, **pro_paper_status()})
                return
            if path == "/api/c-auto-v2-paper":
                self.send_json(200, {"ok": True, **c_auto_v2_paper_status()})
                return
            if path == "/api/c-auto-v2-micro-live":
                self.send_json(200, {"ok": True, **c_auto_v2_micro_live_status()})
                return
            if path == "/api/8-layer-pipeline":
                self.send_json(200, eight_layer_pipeline_status())
                return
            if path == "/api/data-refresh":
                self.send_json(200, {"ok": True, **data_refresh_status()})
                return
            if path == "/api/smartmoney-diffusion":
                self.send_json(200, {"ok": True, **smartmoney_diffusion_status()})
                return
            if path == "/api/download-status":
                self.send_json(200, download_status())
                return
            if path == "/api/monster":
                self.send_json(200, monster_status())
                return
            if path == "/api/monster-orderbook":
                self.send_json(200, {"ok": True, **monster_orderbook_status()})
                return
            if path == "/api/monster-derivatives":
                self.send_json(200, {"ok": True, **monster_derivatives_status()})
                return
            if path == "/api/monster-paper":
                self.send_json(200, {"ok": True, **monster_paper_status()})
                return
            if path == "/api/monster-auto-refresh":
                self.send_json(200, {"ok": True, **monster_auto_refresh_status()})
                return
            if path == "/api/logs":
                self.send_json(200, {"logs": latest_launcher_logs()})
                return
            self.serve_static(path)
        except Exception as exc:
            self.send_json(500, {"ok": False, "error": str(exc), "route": path})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            if path == "/api/start":
                self.send_json(200, self.handle_start())
                return
            if path == "/api/stop":
                self.send_json(200, self.handle_stop())
                return
            if path == "/api/restart":
                payload = self.read_json_body()
                self.handle_stop()
                self.reset_paper_state(payload)
                time.sleep(1.0)
                self.send_json(200, self.handle_start(payload))
                return
            if path == "/api/download-pause":
                payload = self.read_json_body()
                self.send_json(200, pause_download(payload.get("run_id")))
                return
            if path == "/api/download-resume":
                payload = self.read_json_body()
                self.send_json(200, resume_download(payload.get("run_id")))
                return
            if path == "/api/monster-refresh":
                self.send_json(200, refresh_monster())
                return
            if path == "/api/monster-orderbook-start":
                self.send_json(200, start_monster_orderbook())
                return
            if path == "/api/monster-derivatives-start":
                self.send_json(200, start_monster_derivatives())
                return
            if path == "/api/monster-paper-start":
                self.send_json(200, start_monster_paper())
                return
            if path == "/api/monster-auto-refresh-start":
                self.send_json(200, start_monster_auto_refresh())
                return
            if path == "/api/c-auto-v2-micro-live-start":
                payload = self.read_json_body()
                confirm = bool(payload.get("confirm_real")) and bool(payload.get("confirm_competition"))
                self.send_json(200, start_c_auto_v2_micro_live("competition", confirm=confirm))
                return
            if path == "/api/smartmoney-diffusion-start":
                self.send_json(200, start_smartmoney_diffusion())
                return
            if path == "/api/smartmoney-diffusion-stop":
                self.send_json(200, stop_smartmoney_diffusion())
                return
            if path == "/api/c-auto-v2/close-symbol":
                payload = self.read_json_body()
                self.send_json(200, close_c_auto_v2_symbol(payload))
                return
            if path == "/api/c-auto-v2-micro-live/close-symbol":
                payload = self.read_json_body()
                self.send_json(200, close_c_auto_v2_micro_live_symbol(payload))
                return
            if path == "/api/monster-paper/close-symbol":
                payload = self.read_json_body()
                self.send_json(200, close_monster_paper_symbol(payload))
                return
            self.send_json(404, {"ok": False, "error": "unknown route"})
        except ValueError as exc:
            self.send_json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            self.send_json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def handle_start(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload if payload is not None else self.read_json_body()
        data_refresh = start_data_refresh()
        smartmoney_diffusion = start_smartmoney_diffusion()
        env = str(payload.get("env", "personal")).strip()
        mode = str(payload.get("mode", "paper")).strip()
        strategy = str(payload.get("strategy", "core_c_auto_h24_regression_v1")).strip()
        port = normalize_port(payload.get("port", DEFAULT_DASHBOARD_PORT))
        confirm_real = bool(payload.get("confirm_real", False))
        confirm_competition = bool(payload.get("confirm_competition", False))

        if env not in ALLOWED_ENVS:
            raise ValueError(f"unsupported environment: {env}")
        if mode not in ALLOWED_MODES:
            raise ValueError(f"unsupported mode: {mode}")
        option = strategy_option(strategy)
        if option is None:
            raise ValueError(f"unsupported strategy: {strategy}")
        if mode == "real" and not confirm_real:
            raise ValueError("真实交易模式需要明确确认")
        if env == "competition" and mode == "real" and not confirm_competition:
            raise ValueError("比赛真实交易环境需要明确确认")
        kill_switch = clear_launcher_kill_switch()

        if option["kind"] == "professional":
            if mode == "paper":
                daily_review = start_c_auto_daily_review(env)
                result = start_pro_paper(strategy, env)
                result["data_refresh"] = data_refresh
                result["smartmoney_diffusion"] = smartmoney_diffusion
                result["daily_review"] = daily_review
                result["kill_switch"] = kill_switch
                return result
            if not option.get("real_supported"):
                raise ValueError("该 professional 策略尚未通过 live gate，不能启动真实交易")
            raise ValueError("professional live runner 尚未接入 launcher")

        if option["kind"] == "c_auto_v2":
            if mode == "paper":
                daily_review = start_c_auto_daily_review(env)
                result = start_c_auto_v2_paper(env, fresh_start=bool(payload.get("fresh_start", False)))
                result["data_refresh"] = data_refresh
                result["smartmoney_diffusion"] = smartmoney_diffusion
                result["daily_review"] = daily_review
                result["kill_switch"] = kill_switch
                return result
            if env != "competition":
                raise ValueError("C-Auto micro-live 只允许比赛真实交易环境")
            daily_review = start_c_auto_daily_review(env)
            paper = start_c_auto_v2_paper(env, fresh_start=False)
            micro_live = start_c_auto_v2_micro_live(env, confirm=confirm_real and confirm_competition)
            return {
                "ok": True,
                "strategy": strategy,
                "env": env,
                "mode": "real",
                "paper": paper,
                "micro_live": micro_live,
                "data_refresh": data_refresh,
                "smartmoney_diffusion": smartmoney_diffusion,
                "daily_review": daily_review,
                "kill_switch": kill_switch,
            }

        if mode == "paper":
            raise ValueError("legacy 策略没有接入纸面交易模式；请选择 professional 策略")

        script_env = "live" if env == "competition" else env

        result = run_script(
            [str(ROOT_DIR / "manage_local.sh"), "start", strategy, str(port), script_env],
            f"start_{strategy}_{env}",
        )
        result.update(
            {
                "ok": True,
                "strategy": strategy,
                "env": env,
                "mode": mode,
                "dashboard_port": port,
                "dashboard_url": f"http://127.0.0.1:{port}/",
                "yolo_url": f"http://127.0.0.1:{port}/yolo",
                "data_refresh": data_refresh,
                "smartmoney_diffusion": smartmoney_diffusion,
                "kill_switch": kill_switch,
            }
        )
        return result

    def handle_stop(self) -> dict[str, Any]:
        kill_switch = activate_kill_switch("launcher pause all")
        result = run_script([str(ROOT_DIR / "manage_local.sh"), "stop"], "stop")
        pro = stop_pro_paper()
        c_auto_v2 = stop_c_auto_v2_paper()
        c_auto_v2_micro_live = stop_c_auto_v2_micro_live()
        data_refresh = stop_data_refresh()
        smartmoney_diffusion = stop_smartmoney_diffusion()
        daily_review = stop_c_auto_daily_review()
        order_cancel = cancel_all_open_swap_orders()
        result.update(
            {
                "ok": True,
                "kill_switch": kill_switch,
                "order_cancel": order_cancel,
                "pro_paper": pro,
                "c_auto_v2_paper": c_auto_v2,
                "c_auto_v2_micro_live": c_auto_v2_micro_live,
                "data_refresh": data_refresh,
                "smartmoney_diffusion": smartmoney_diffusion,
                "daily_review": daily_review,
            }
        )
        return result

    def reset_paper_state(self, payload: dict[str, Any]) -> None:
        strategy = str(payload.get("strategy", "")).strip()
        mode = str(payload.get("mode", "paper")).strip()
        env = str(payload.get("env", "personal")).strip()
        if mode != "paper" or strategy != C_AUTO_V2_STRATEGY_ID:
            return
        archive_c_auto_v2_paper_session(C_AUTO_V2_STATE_ID, env, "launcher_restart")

    def status_payload(self) -> dict[str, Any]:
        pids = pid_snapshot()
        return {
            "ok": True,
            "root": str(ROOT_DIR),
            "default_dashboard_port": DEFAULT_DASHBOARD_PORT,
            "default_dashboard_url": f"http://127.0.0.1:{DEFAULT_DASHBOARD_PORT}/",
            "default_yolo_url": f"http://127.0.0.1:{DEFAULT_DASHBOARD_PORT}/yolo",
            "summary": active_summary(),
            "pids": pids,
            "pro_paper": pro_paper_status(),
            "c_auto_v2_paper": c_auto_v2_paper_status(),
            "c_auto_v2_micro_live": c_auto_v2_micro_live_status(),
            "data_refresh": data_refresh_status(),
            "smartmoney_diffusion": smartmoney_diffusion_status(),
            "daily_review": c_auto_daily_review_status(),
            "kill_switch": kill_switch_status(),
            "launcher_logs": latest_launcher_logs(),
        }

    def serve_static(self, path: str) -> None:
        rel = "index.html" if path == "/" else path.lstrip("/")
        static_path = (STATIC_DIR / rel).resolve()
        if not str(static_path).startswith(str(STATIC_DIR.resolve())):
            self.send_error(403)
            return
        if not static_path.exists() or not static_path.is_file():
            self.send_error(404)
            return
        content = static_path.read_bytes()
        ctype = mimetypes.guess_type(str(static_path))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(content)))
        if static_path.suffix in {".html", ".js", ".css"}:
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, fmt: str, *args: Any) -> None:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with (LOGS_DIR / "launcher_access.log").open("a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {self.address_string()} {fmt % args}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Local OKX trading launcher")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    args = parser.parse_args()

    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    (CONTROL_DIR / "launcher.pid").write_text(str(os.getpid()))
    if hasattr(signal, "SIGCHLD"):
        signal.signal(signal.SIGCHLD, signal.SIG_IGN)

    server = ThreadingHTTPServer((args.host, args.port), LauncherHandler)
    print(f"launcher: http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
