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
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo


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
RESEARCH_SLEEVES_DIR = LOGS_DIR / "research_sleeves"
OWNERSHIP_DIR = LOGS_DIR / "ownership"
OPERATION_LOG_PATH = LOGS_DIR / "launcher_operations.jsonl"
PYTHON_BIN = os.environ.get("OKX_TRADING_SYSTEM_PYTHON", sys.executable)
OKX_ENV_CREDENTIAL_KEYS = {
    "OKX_API_KEY",
    "OKX_API_SECRET",
    "OKX_SECRET_KEY",
    "OKX_PASSPHRASE",
}
KILL_SWITCH_PATH = CONTROL_DIR / "kill.switch"
STOP_GRACE_SEC = 3.0

from registry import StrategyRegistry
from runtime.environment_runner import EnvironmentRunner


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
START_READINESS_MIN_SYMBOLS = 40
START_READINESS_TIMEFRAMES = ("1h", "4h")
START_READINESS_DERIVATIVE_KINDS = ("funding", "open_interest", "long_short")
KNOWN_STRATEGY_SLEEVES = [
    {
        "strategy_id": "c_auto_v2_cross_section",
        "display_name": "C-Auto Cross Section",
        "paper_source": "paper_competition",
        "paper_role": "主 sleeve：截面回归择币",
    },
    {
        "strategy_id": "trend_pullback_reversal_long",
        "display_name": "Trend Pullback Reversal",
        "paper_source": "paper_competition",
        "paper_role": "4h 定趋势，1h 回调/分型确认",
    },
    {
        "strategy_id": "trend_pullback_reversal_quality_top20_v1",
        "display_name": "Trend Pullback Quality Top20",
        "paper_source": "research_competition",
        "paper_role": "4h 定趋势 + 1h 回调反转；质量分 top20%；5% TP / 1.5% SL；需 1h + 4h 数据",
        "runtime_rule": "预算50U；单笔10U；最多同时3笔；同币种冲突低于 cluster_elite_quality60、高于 rank_top1",
    },
    {
        "strategy_id": "trend_pullback_reversal_rank_top1_v1",
        "display_name": "Trend Pullback Rank Top1",
        "paper_source": "research_competition",
        "paper_role": "4h 定趋势 + 1h 回调反转；每轮全市场 top1；5% TP / 2% SL；需 1h + 4h 数据",
        "runtime_rule": "预算50U；单笔10U；最多同时3笔；同币种冲突优先级第三",
    },
    {
        "strategy_id": "trend_pullback_reversal_cluster_elite_quality60_v1",
        "display_name": "Trend Pullback Cluster Elite Quality60",
        "paper_source": "research_competition",
        "paper_role": "4h 定趋势 + 1h 回调反转；滚动 elite cluster + quality>=0.60；5% TP / 0.8% SL；需 1h + 4h + funding/OI/LS 数据",
        "runtime_rule": "预算50U；单笔10U；最多同时3笔；同币种冲突优先级第一",
    },
    {
        "strategy_id": "daily_fib_support_rebound_long",
        "display_name": "Daily Fib Support Rebound",
        "paper_source": "paper_competition",
        "paper_role": "关键支撑位触达、未破、收回",
    },
    {
        "strategy_id": "deriv_oi_compression_breakout",
        "display_name": "OI Compression Breakout",
        "paper_source": "paper_competition",
        "paper_role": "衍生品 OI 压缩突破",
    },
    {
        "strategy_id": "deriv_crowding_reversal",
        "display_name": "Crowding Reversal",
        "paper_source": "paper_competition",
        "paper_role": "拥挤反转 sleeve",
    },
    {
        "strategy_id": "btc_weekly_swing_3x",
        "display_name": "BTC Weekly Swing 3x",
        "paper_source": "research_competition",
        "paper_role": "BTC 周线 13w 突破，100U 比赛环境预算，3x",
    },
    {
        "strategy_id": "btc_daily_breakout_swing",
        "display_name": "BTC Daily Breakout Swing",
        "paper_source": "research_competition",
        "paper_role": "BTC 日线 80d 突破 + 周线趋势过滤，100U 比赛环境预算，2x",
    },
    {
        "strategy_id": "us_equity_token_equity_momentum",
        "display_name": "美股策略-高质量美股动量",
        "paper_source": "research_competition",
        "paper_role": "OKX 美股合约：AMZN/GOOGL/NVDA 真实美股动量精选池，20U 比赛环境预算，1x",
    },
    {
        "strategy_id": "us_equity_token_okx_momentum",
        "display_name": "美股策略-OKX自身动量精选版",
        "paper_source": "research_competition",
        "paper_role": "OKX 美股合约：COIN/HOOD/AMZN/GOOGL token 自身动量精选池，25U 比赛环境预算，1x",
    },
]
DISPLAY_TZ = ZoneInfo("Asia/Shanghai")
TIME_FIELD_NAMES = {
    "archived_at",
    "checked_at",
    "completed_at",
    "created_at",
    "cycle_started_at",
    "entry_ts",
    "exit_ts",
    "finished_at",
    "heartbeat_at",
    "last_entry_scan_ts",
    "last_entry_ts",
    "last_rebalance_ts",
    "latest_market_ts",
    "modified",
    "observed_at",
    "sample_ts",
    "since",
    "started_at",
    "target_end",
    "timestamp",
    "ts",
    "updated_at",
}


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


def beijing_time(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            ts = float(value)
            if ts > 1_000_000_000_000:
                ts /= 1000.0
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        elif isinstance(value, str):
            text = value.strip()
            if text.isdigit():
                ts = float(text)
                if ts > 1_000_000_000_000:
                    ts /= 1000.0
                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            else:
                normalized = text.replace("Z", "+00:00")
                dt = datetime.fromisoformat(normalized)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
        elif isinstance(value, datetime):
            dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        else:
            return None
    except Exception:
        return None
    return dt.astimezone(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S 北京时间")


def utc_age_seconds(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, datetime):
            dt = value
        else:
            dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None
    return max(0.0, (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds())


def with_display_times(value: Any, *, root: bool = True) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            out[text_key] = with_display_times(item, root=False)
            if text_key.endswith("_bj"):
                continue
            if text_key in TIME_FIELD_NAMES or text_key.endswith("_at") or text_key.endswith("_ts"):
                converted = beijing_time(item)
                if converted is not None:
                    out[f"{text_key}_bj"] = converted
        if root:
            out.setdefault("display_timezone", "Asia/Shanghai")
            out.setdefault("display_timezone_label", "北京时间")
        return out
    if isinstance(value, list):
        return [with_display_times(item, root=False) for item in value]
    if isinstance(value, tuple):
        return [with_display_times(item, root=False) for item in value]
    return value


def process_alive(pid: str | int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def terminate_process(pid: str | int | None, *, grace_sec: float = STOP_GRACE_SEC) -> dict[str, Any]:
    """Terminate a runner process and escalate if it ignores SIGTERM."""
    if not pid:
        return {"pid": pid, "terminated": False, "reason": "missing_pid"}
    try:
        pid_int = int(pid)
    except Exception:
        return {"pid": pid, "terminated": False, "reason": "invalid_pid"}
    if not process_alive(pid_int):
        return {"pid": pid_int, "terminated": True, "already_stopped": True}

    result: dict[str, Any] = {"pid": pid_int, "terminated": False, "signal": "TERM", "escalated": False}
    try:
        try:
            os.killpg(pid_int, signal.SIGTERM)
            result["target"] = "process_group"
        except OSError:
            os.kill(pid_int, signal.SIGTERM)
            result["target"] = "process"
    except OSError as exc:
        result["error"] = str(exc)
        return result

    deadline = time.time() + max(0.1, grace_sec)
    while time.time() < deadline:
        if not process_alive(pid_int):
            result["terminated"] = True
            return result
        time.sleep(0.1)

    result["escalated"] = True
    result["signal"] = "KILL"
    try:
        try:
            os.killpg(pid_int, signal.SIGKILL)
        except OSError:
            os.kill(pid_int, signal.SIGKILL)
    except OSError as exc:
        result["error"] = str(exc)
    time.sleep(0.2)
    result["terminated"] = not process_alive(pid_int)
    return result


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
        "ownership_reconcile": find_ownership_reconcile_processes(),
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
    options: list[dict[str, Any]] = []
    try:
        registry = StrategyRegistry()
        for record in registry.list_strategies():
            runner = record.runtime.runner
            options.append(
                {
                    "strategy_id": record.strategy_id,
                    "name": record.name,
                    "book": record.book,
                    "status": record.status,
                    "kind": "c_auto_v2" if runner == "c_auto_v2_micro_live" else "professional",
                    "description": record.description,
                    "live_enabled": record.live_enabled,
                    "live_allocation_pct": record.live_allocation_pct,
                    "default_parameter_set_id": record.default_parameter_set_id,
                    "paper_supported": True,
                    "real_supported": bool(record.live_enabled and record.status == "live"),
                    "primary": record.strategy_id == C_AUTO_V2_STRATEGY_ID,
                    "runtime": record.runtime.to_dict(),
                    "data_dependencies": [item.to_dict() for item in record.data_dependencies],
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
                "real_supported": False,
                "primary": False,
                "retired": True,
            }
        )
    return options


def strategy_option(strategy_id: str) -> dict[str, Any] | None:
    for item in strategy_options():
        if item["strategy_id"] == strategy_id:
            return item
    return None


def registry_payload() -> dict[str, Any]:
    path = ENGINE_DIR / "config" / "strategy_registry.json"
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def registry_parameter_params(parameter_set_id: str | None) -> dict[str, Any]:
    if not parameter_set_id:
        return {}
    for item in registry_payload().get("parameter_sets", []):
        if str(item.get("parameter_set_id") or "") == parameter_set_id:
            return dict(item.get("params") or {})
    return {}


def environment_runtime_strategies(environment: str) -> list[dict[str, Any]]:
    return [record.to_dict() for record in StrategyRegistry().runnable_strategies(environment)]


def environment_runtime_plan(environment: str) -> dict[str, Any]:
    if environment not in {"personal", "competition"}:
        return {"ok": False, "environment": environment, "error": "unsupported_environment", "plans": []}
    runner = EnvironmentRunner()
    rows = []
    for plan in runner.plan(environment):
        existing = [dict(item) for item in runner.existing_processes(plan)]
        rows.append(
            {
                "strategy_id": plan.strategy_id,
                "environment": plan.environment,
                "runner": plan.runner,
                "state_id": plan.state_id,
                "okx_profile": plan.okx_profile,
                "priority": plan.priority,
                "running": bool(existing),
                "processes": existing,
                "readiness": {
                    "ok": plan.readiness.ok,
                    "checked": list(plan.readiness.checked),
                    "errors": list(plan.readiness.errors),
                },
            }
        )
    return {"ok": True, "environment": environment, "plans": rows}


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


def find_research_sleeve_processes(strategy_id: str | None = None, environment: str | None = None) -> list[dict[str, Any]]:
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
        if not stripped or "scripts/run_research_sleeve_paper.py" not in stripped:
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
        if environment and found_env != environment:
            continue
        matches.append({"pid": pid, "command": command, "strategy_id": found_strategy, "environment": found_env})
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


def okx_profile_for_environment(environment: str) -> str:
    return "live" if environment == "competition" else environment


def assert_strategy_environment_exclusive(strategy_id: str, environment: str) -> None:
    """Prevent the same strategy from running in both personal and competition."""
    if environment not in {"personal", "competition"}:
        return
    conflicts: list[dict[str, Any]] = []
    c_auto_aliases = {C_AUTO_V2_STRATEGY_ID, "c_auto_v2_cross_section"}
    if strategy_id in c_auto_aliases:
        for proc in find_c_auto_v2_micro_live_processes():
            proc_env = str(proc.get("environment") or "")
            if proc_env in {"personal", "competition"} and proc_env != environment:
                conflicts.append({"source": "c_auto_v2_micro_live", **proc})
        for proc in find_c_auto_v2_paper_processes(C_AUTO_V2_STATE_ID):
            proc_env = str(proc.get("environment") or "")
            if proc_env in {"personal", "competition"} and proc_env != environment:
                conflicts.append({"source": "c_auto_v2_paper", **proc})
    else:
        for proc in find_research_sleeve_processes(strategy_id=strategy_id):
            proc_env = str(proc.get("environment") or "")
            if proc_env in {"personal", "competition"} and proc_env != environment:
                conflicts.append({"source": "research_sleeve", **proc})
        for proc in find_pro_paper_processes(strategy_id=strategy_id):
            proc_env = str(proc.get("environment") or "")
            if proc_env in {"personal", "competition"} and proc_env != environment:
                conflicts.append({"source": "pro_paper", **proc})
    if conflicts:
        detail = {"strategy_id": strategy_id, "environment": environment, "conflicts": conflicts}
        append_operation("exclusive_strategy_gate", environment, "blocked", detail)
        conflict_envs = sorted({str(item.get("environment")) for item in conflicts if item.get("environment")})
        raise ValueError(
            f"strategy {strategy_id} is already running in {', '.join(conflict_envs)}; "
            "personal and competition cannot run the same strategy simultaneously"
        )


def command_profile(command: list[str]) -> str | None:
    for idx, part in enumerate(command):
        if part in {"--profile", "--okx-profile"} and idx + 1 < len(command):
            return command[idx + 1]
        for key in ("--profile=", "--okx-profile="):
            if part.startswith(key):
                return part.split("=", 1)[1]
    return None


def okx_command_env(profile: str | None) -> dict[str, str]:
    env = os.environ.copy()
    if profile and profile != "live":
        for key in OKX_ENV_CREDENTIAL_KEYS:
            env.pop(key, None)
    return env


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
    assert_strategy_environment_exclusive(strategy_id, environment)
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


def start_research_sleeve_strategy(record: dict[str, Any], environment: str, confirm: bool) -> dict[str, Any]:
    if environment not in {"competition", "personal"}:
        raise ValueError(f"research sleeve does not support environment: {environment}")
    if not confirm:
        append_operation(
            "start_research_sleeve",
            environment,
            "rejected",
            {"strategy_id": record.get("strategy_id"), "reason": "confirm_real_required"},
        )
        raise ValueError("research sleeve live execution requires confirm_real=true")
    strategy_id = str(record.get("strategy_id") or "")
    if not strategy_id:
        raise ValueError("missing strategy_id")
    existing = find_research_sleeve_processes(strategy_id=strategy_id, environment=environment)
    if existing:
        append_operation("start_research_sleeve", environment, "already_running", {"strategy_id": strategy_id, "processes": existing})
        return {"ok": True, "already_running": True, "strategy": strategy_id, "environment": environment, "processes": existing}
    assert_strategy_environment_exclusive(strategy_id, environment)
    params = registry_parameter_params(str(record.get("default_parameter_set_id") or ""))
    capital = float(params.get("runtime_budget_usdt") or params.get("capital_usdt") or 50.0)
    state_id = str((record.get("runtime") or {}).get("state_id") or strategy_id)
    stop_path = CONTROL_DIR / f"research_sleeve_{state_id}_{environment}.stop"
    try:
        stop_path.unlink()
    except FileNotFoundError:
        pass
    result = run_script(
        [
            "python3",
            "scripts/run_research_sleeve_paper.py",
            "--strategy-id",
            strategy_id,
            "--state-id",
            state_id,
            "--environment",
            environment,
            "--initial-capital",
            f"{capital:g}",
            "--execution",
            "live",
            "--okx-profile",
            okx_profile_for_environment(environment),
            "--parameter-set-id",
            str(record.get("default_parameter_set_id") or ""),
            "--loop",
            "--interval-sec",
            str((record.get("runtime") or {}).get("interval_sec") or 300),
        ],
        f"research_sleeve_{strategy_id}_{environment}",
    )
    result.update(
        {
            "ok": True,
            "strategy": strategy_id,
            "environment": environment,
            "mode": "real",
            "source_mode": "research_sleeve",
            "okx_profile": okx_profile_for_environment(environment),
        }
    )
    append_operation("start_research_sleeve", environment, "accepted", {"strategy_id": strategy_id, "pid": result.get("pid"), "log_path": result.get("log_path")})
    return result


def start_registered_strategy(record: dict[str, Any], environment: str, confirm: bool) -> dict[str, Any]:
    strategy_id = str(record.get("strategy_id") or "")
    runner = str((record.get("runtime") or {}).get("runner") or record.get("module") or "")
    if strategy_id == C_AUTO_V2_STRATEGY_ID or runner == "c_auto_v2_micro_live":
        return start_c_auto_v2_micro_live(environment, confirm=confirm)
    if runner in {"research_sleeve_live", "scripts.run_research_sleeve_paper", "scripts.run_trend_pullback_reversal_variants"}:
        return start_research_sleeve_strategy(record, environment, confirm=confirm)
    raise ValueError(f"unsupported runtime runner for {strategy_id}: {runner or 'missing'}")


def registered_strategy_already_running(record: dict[str, Any], environment: str) -> bool:
    strategy_id = str(record.get("strategy_id") or "")
    runner = str((record.get("runtime") or {}).get("runner") or record.get("module") or "")
    if strategy_id == C_AUTO_V2_STRATEGY_ID or runner == "c_auto_v2_micro_live":
        return bool(
            proc
            for proc in find_c_auto_v2_micro_live_processes()
            if str(proc.get("environment") or "") == environment
        )
    if runner in {"research_sleeve_live", "scripts.run_research_sleeve_paper", "scripts.run_trend_pullback_reversal_variants"}:
        return bool(find_research_sleeve_processes(strategy_id=strategy_id, environment=environment))
    return False


def registered_strategy_requires_global_readiness(record: dict[str, Any]) -> bool:
    strategy_id = str(record.get("strategy_id") or "")
    runner = str((record.get("runtime") or {}).get("runner") or record.get("module") or "")
    module = str(record.get("module") or "")
    if strategy_id == C_AUTO_V2_STRATEGY_ID or runner == "c_auto_v2_micro_live":
        return True
    if strategy_id.startswith("trend_pullback_reversal_") or module == "scripts.run_trend_pullback_reversal_variants":
        return True
    return False


def start_environment_strategies(environment: str, confirm: bool = False) -> dict[str, Any]:
    if environment not in {"personal", "competition"}:
        raise ValueError("环境启动只支持 personal 或 competition")
    if not confirm:
        append_operation("start_environment", environment, "rejected", {"reason": "confirm_real_required"})
        raise ValueError("environment runner requires confirm_real=true")
    data_refresh = start_data_refresh()
    ownership_reconcile = start_ownership_reconcile_scheduler()
    runner = EnvironmentRunner()
    plans = runner.plan(environment)
    if not plans:
        append_operation("start_environment", environment, "blocked", {"reason": "no_runtime_strategies_registered"})
        raise ValueError(f"没有注册允许在 {environment} 环境运行的策略")
    preflight_status = runner.status(environment, plans=plans)
    account_truth = account_reconciliation_snapshot(environment)
    ownership_truth = refresh_ownership_reconciliation(environment, max_age_sec=0)
    if ownership_truth and ownership_truth.get("ok") is False:
        detail = {"reason": "ownership_reconciliation_failed", "ownership_truth": ownership_truth}
        append_operation("start_environment", environment, "blocked", detail)
        raise ValueError(f"{environment} ownership 对账失败，先完成 account/ownership reconcile 再启动")
    internal_open_positions = sum(
        int(((row.get("position") or {}).get("open_positions") or 0))
        for row in preflight_status.get("strategies", [])
        if isinstance(row, dict)
    )
    exchange_open_positions = int(account_truth.get("position_count") or 0)
    if exchange_open_positions > internal_open_positions:
        detail = {
            "reason": "unknown_exchange_positions",
            "exchange_open_positions": exchange_open_positions,
            "internal_open_positions": internal_open_positions,
            "account_truth": account_truth,
        }
        append_operation("start_environment", environment, "blocked", detail)
        raise ValueError(
            f"{environment} 账户有 {exchange_open_positions} 个交易所持仓，但 runner 只识别 {internal_open_positions} 个；"
            "先清仓/对账后再启动"
        )

    started: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    plan_rows: list[dict[str, Any]] = []
    for plan in plans:
        plan_row = {
            "strategy_id": plan.strategy_id,
            "environment": plan.environment,
            "runner": plan.runner,
            "state_id": plan.state_id,
            "okx_profile": plan.okx_profile,
            "priority": plan.priority,
            "command": list(plan.command),
            "readiness": {
                "ok": plan.readiness.ok,
                "checked": list(plan.readiness.checked),
                "errors": list(plan.readiness.errors),
            },
        }
        plan_rows.append(plan_row)
        if not plan.readiness.ok:
            errors.append({"strategy_id": plan.strategy_id, "error": "; ".join(plan.readiness.errors) or "data_not_ready"})
            continue
        existing = list(runner.existing_processes(plan))
        if existing:
            started.append(
                {
                    "ok": True,
                    "already_running": True,
                    "strategy": plan.strategy_id,
                    "environment": environment,
                    "processes": [dict(item) for item in existing],
                    "mode": "environment_runner",
                }
            )
            continue
        try:
            result = run_script(list(plan.command), f"environment_{environment}_{plan.strategy_id}")
            result.update(
                {
                    "ok": True,
                    "strategy": plan.strategy_id,
                    "environment": environment,
                    "mode": "environment_runner",
                    "runner": plan.runner,
                    "state_id": plan.state_id,
                    "okx_profile": plan.okx_profile,
                }
            )
            started.append(result)
        except Exception as exc:
            errors.append({"strategy_id": plan.strategy_id, "error": str(exc)})
    result = {
        "ok": not errors,
        "environment": environment,
        "mode": "environment_runner",
        "plans": plan_rows,
        "strategies": [plan.strategy_id for plan in plans],
        "started": started,
        "errors": errors,
        "data_refresh": data_refresh,
        "ownership_reconcile": ownership_reconcile,
    }
    result["runner_status"] = runner.write_status(environment, plans=plans)
    append_operation("start_environment", environment, "accepted" if not errors else "partial_error", result)
    if errors and not started:
        raise ValueError("; ".join(f"{item['strategy_id']}: {item['error']}" for item in errors))
    return result


def c_auto_v2_paper_status(state_id: str = "fixed1000_conservative", environment: str | None = None) -> dict[str, Any]:
    if environment in {None, "competition"}:
        live_status = c_auto_v2_micro_live_status(state_id="micro_live_competition", environment="competition")
        if live_status.get("available"):
            live_status = dict(live_status)
            live_status["paper_alias"] = True
            live_status["mode"] = "paper_competition_micro_live"
            live_status["display_name"] = "Paper = competition micro-live"
            return live_status
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
    stop_path = CONTROL_DIR / f"c_auto_v2_paper_{prefix}.stop"
    state = read_json(state_path) or {}
    scheduler = read_json(scheduler_path) or {}
    if not state and not scheduler and not processes:
        return {"available": False, "state_id": state_id, "message": "no c-auto v2 paper state found"}
    display_scheduler = dict(scheduler)
    if not processes and display_scheduler.get("scheduler_status") == "running":
        display_scheduler["scheduler_status"] = str(state.get("runner_status") or "stopped")
    if stop_path.exists():
        display_scheduler["scheduler_status"] = "stopped"
        display_scheduler["stop_file"] = str(stop_path.relative_to(ROOT_DIR))
        display_scheduler["stopped_at"] = stop_path.read_text(errors="ignore").strip()
    out = dict(state)
    out.update(
        {
            "available": True,
            "running": bool(processes) and not stop_path.exists(),
            "processes": processes,
            "scheduler": display_scheduler,
            "stop_file": str(stop_path.relative_to(ROOT_DIR)) if stop_path.exists() else None,
            "state_path": str(state_path.relative_to(ROOT_DIR)) if state_path.exists() else None,
            "scheduler_status_path": str(scheduler_path.relative_to(ROOT_DIR)) if scheduler_path.exists() else None,
        }
    )
    _mask_stale_strategy_state(out, state)
    return out


def c_auto_v2_micro_live_status(state_id: str | None = None, environment: str | None = None) -> dict[str, Any]:
    processes = find_c_auto_v2_micro_live_processes(state_id)
    if state_id is None:
        state_id = str((processes[0] or {}).get("state_id") or "micro_live_competition") if processes else "micro_live_competition"
    if environment is None:
        process_env = next((str(proc.get("environment") or "") for proc in processes if proc.get("environment")), "")
        if process_env in ALLOWED_ENVS:
            environment = process_env
        else:
            candidates = sorted(C_AUTO_V2_MICRO_LIVE_DIR.glob(f"{state_id}_*_scheduler.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            environment = "competition"
            for path in candidates:
                suffix = path.name.removeprefix(f"{state_id}_").removesuffix("_scheduler.json")
                if suffix in ALLOWED_ENVS:
                    environment = suffix
                    break
    prefix = f"{state_id}_{environment}"
    state_path = C_AUTO_V2_MICRO_LIVE_DIR / f"{prefix}.json"
    scheduler_path = C_AUTO_V2_MICRO_LIVE_DIR / f"{prefix}_scheduler.json"
    stop_path = CONTROL_DIR / f"c_auto_v2_micro_live_{prefix}.stop"
    state = read_json(state_path) or {}
    scheduler = read_json(scheduler_path) or {}
    if not state and not scheduler and not processes:
        return {"available": False, "state_id": state_id, "message": "no c-auto v2 micro-live state found"}
    display_scheduler = dict(scheduler)
    if not processes and display_scheduler.get("scheduler_status") == "running":
        display_scheduler["scheduler_status"] = str(state.get("runner_status") or "stopped")
    if stop_path.exists():
        display_scheduler["scheduler_status"] = "stopped"
        display_scheduler["stop_file"] = str(stop_path.relative_to(ROOT_DIR))
        display_scheduler["stopped_at"] = stop_path.read_text(errors="ignore").strip()
    out = dict(state)
    out.update(
        {
            "available": True,
            "running": bool(processes) and not stop_path.exists(),
            "processes": processes,
            "scheduler": display_scheduler,
            "stop_file": str(stop_path.relative_to(ROOT_DIR)) if stop_path.exists() else None,
            "state_path": str(state_path.relative_to(ROOT_DIR)) if state_path.exists() else None,
            "scheduler_status_path": str(scheduler_path.relative_to(ROOT_DIR)) if scheduler_path.exists() else None,
        }
    )
    _mask_stale_strategy_state(out, state)
    return out


def _mask_stale_strategy_state(out: dict[str, Any], raw_state: dict[str, Any]) -> None:
    """Keep stopped strategy cache files from looking like live positions."""
    if out.get("running"):
        out["display_status"] = "running"
        return
    raw_positions = raw_state.get("positions") if isinstance(raw_state, dict) else {}
    cached_open_positions = len(raw_positions) if isinstance(raw_positions, dict) else 0
    out["display_status"] = "stopped"
    out["stale_without_process"] = cached_open_positions > 0 or bool(out.get("scheduler"))
    out["cached_open_positions"] = cached_open_positions
    out["positions"] = {}
    out["open_positions"] = 0
    out["open_risk"] = 0.0
    out["unrealized_pnl"] = 0.0
    account_truth = out.pop("account_truth", None)
    if isinstance(account_truth, dict):
        out["cached_account_truth"] = {
            "stale_without_process": True,
            "checked_at": account_truth.get("checked_at"),
            "profile": account_truth.get("profile"),
            "position_count": len(account_truth.get("positions") or {}),
            "open_order_count": sum(len(v) for v in (account_truth.get("open_orders") or {}).values() if isinstance(v, list)),
            "algo_order_count": sum(len(v) for v in (account_truth.get("algo_orders") or {}).values() if isinstance(v, list)),
        }


def strategy_performance_status() -> dict[str, Any]:
    refresh_ownership_reconciliation("personal")
    refresh_ownership_reconciliation("competition")
    sources = [
        ("paper_competition", C_AUTO_V2_MICRO_LIVE_DIR, "micro_live_competition_competition"),
        ("micro_live_personal", C_AUTO_V2_MICRO_LIVE_DIR, "micro_live_personal_personal"),
        ("research_competition", RESEARCH_SLEEVES_DIR, "btc_weekly_swing_3x_competition"),
        ("research_competition", RESEARCH_SLEEVES_DIR, "btc_daily_breakout_swing_competition"),
        ("research_competition", RESEARCH_SLEEVES_DIR, "us_equity_token_equity_momentum_competition"),
        ("research_competition", RESEARCH_SLEEVES_DIR, "us_equity_token_dislocation_reversion_competition"),
        ("research_competition", RESEARCH_SLEEVES_DIR, "us_equity_token_okx_momentum_competition"),
    ]
    running_sources = _running_strategy_sources()
    strategies: dict[str, dict[str, Any]] = {}
    for source_id, directory, prefix in sources:
        ledger = iter_jsonl(directory / f"{prefix}_ledger.jsonl")
        equity = iter_jsonl(directory / f"{prefix}_equity.jsonl")
        state = read_json(directory / f"{prefix}.json") or {}
        strategy_ids = _merge_strategy_ledger(strategies, source_id, ledger)
        _merge_strategy_positions(strategies, source_id, state.get("positions") or {})
        _merge_strategy_equity(strategies, source_id, equity, state, strategy_ids)
    _merge_ownership_performance(strategies, "personal")
    _merge_ownership_performance(strategies, "competition")
    _merge_known_strategy_sleeves(strategies, running_sources)
    rows = []
    for strategy_id, row in strategies.items():
        closed = int(row.get("closed_trades") or 0)
        wins = int(row.get("wins") or 0)
        row["win_rate"] = wins / closed if closed > 0 else None
        row["pnl"] = float(row.get("realized_pnl") or 0.0) + float(row.get("unrealized_pnl") or 0.0)
        row["running_sources"] = [
            source
            for source in row.get("sources") or []
            if _is_strategy_source_running(str(row.get("strategy_id") or ""), source, running_sources)
        ]
        row["running"] = bool(row["running_sources"])
        row["series"] = sorted(row.get("series") or [], key=lambda item: str(item.get("ts") or ""))
        rows.append(row)
    rows.sort(key=lambda item: (not bool(item.get("running")), -float(item.get("pnl") or 0.0), str(item.get("strategy_id") or "")))
    running_paper = sum(
        1
        for row in rows
        if row.get("running") and any(source in {"paper_competition", "research_competition", "legacy_paper", "monster_paper"} for source in row.get("sources") or [])
    )
    return {"ok": True, "updated_at": datetime.now(timezone.utc).isoformat(), "running_paper_strategies": running_paper, "strategies": rows}


def _merge_ownership_performance(strategies: dict[str, dict[str, Any]], environment: str) -> None:
    source_id = f"accounting_{environment}"
    status = read_json(OWNERSHIP_DIR / environment / "performance_status.json") or {}
    for perf in status.get("strategies") or []:
        if not isinstance(perf, dict):
            continue
        strategy_id = str(perf.get("strategy_id") or "unknown")
        row = _strategy_row(strategies, strategy_id)
        if source_id not in row["sources"]:
            row["sources"].append(source_id)
        row["realized_pnl"] = float(row.get("realized_pnl") or 0.0) + float(_safe_float(perf.get("net_pnl_usdt")) or 0.0)
        row["closed_trades"] = int(row.get("closed_trades") or 0) + int(perf.get("closed_fills") or 0)
        row["wins"] = int(row.get("wins") or 0) + int(perf.get("wins") or 0)
        row["losses"] = int(row.get("losses") or 0) + int(perf.get("losses") or 0)
        row["open_positions"] = int(row.get("open_positions") or 0) + int(perf.get("open_positions") or 0)
        row["accounting"] = {
            "environment": environment,
            "updated_at": status.get("updated_at") or status.get("generated_at"),
            "exchange_fills": perf.get("exchange_fills"),
            "exchange_bills": perf.get("exchange_bills"),
            "exchange_fees_usdt": perf.get("exchange_fees_usdt"),
            "bill_fees_usdt": perf.get("bill_fees_usdt"),
            "bill_pnl_usdt": perf.get("bill_pnl_usdt"),
            "unmatched_fills": status.get("unmatched_fills"),
            "unmatched_bills": status.get("unmatched_bills"),
        }


def _running_strategy_sources() -> set[str]:
    running: set[str] = set()
    for proc in find_c_auto_v2_micro_live_processes():
        env = str(proc.get("environment") or "")
        if env == "competition":
            running.add("paper_competition")
        elif env == "personal":
            running.add("micro_live_personal")
    if [
        proc
        for proc in find_c_auto_v2_paper_processes()
        if not (CONTROL_DIR / f"c_auto_v2_paper_{proc.get('state_id')}_{proc.get('environment')}.stop").exists()
    ]:
        running.add("legacy_paper")
    if [p for p in find_monster_processes() if "run_monster_paper.py" in p.get("command", "")]:
        running.add("monster_paper")
    if find_research_sleeve_processes(environment="competition"):
        running.add("research_competition")
    if find_research_sleeve_processes(environment="personal"):
        running.add("research_personal")
    return running


def _merge_known_strategy_sleeves(strategies: dict[str, dict[str, Any]], running_sources: set[str]) -> None:
    for meta in KNOWN_STRATEGY_SLEEVES:
        strategy_id = str(meta["strategy_id"])
        row = _strategy_row(strategies, strategy_id)
        row.setdefault("display_name", meta.get("display_name") or strategy_id)
        row.setdefault("paper_role", meta.get("paper_role") or "")
        row.setdefault("runtime_rule", meta.get("runtime_rule") or "")
        row["paper_supported"] = True
        paper_source = str(meta.get("paper_source") or "")
        if paper_source:
            row["paper_source"] = paper_source
            if _is_strategy_source_running(strategy_id, paper_source, running_sources) and paper_source not in row["sources"]:
                row["sources"].append(paper_source)


def _is_strategy_source_running(strategy_id: str, source: str, running_sources: set[str]) -> bool:
    if source == "research_competition":
        return bool(find_research_sleeve_processes(strategy_id=strategy_id, environment="competition"))
    if source == "research_personal":
        return bool(find_research_sleeve_processes(strategy_id=strategy_id, environment="personal"))
    return source in running_sources


def _strategy_key(item: dict[str, Any], fallback: str) -> str:
    for key in ("source_strategy_id", "strategy_id", "strategy", "signal_family"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return fallback


def _strategy_row(strategies: dict[str, dict[str, Any]], strategy_id: str) -> dict[str, Any]:
    if strategy_id not in strategies:
        strategies[strategy_id] = {
            "strategy_id": strategy_id,
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "closed_trades": 0,
            "wins": 0,
            "losses": 0,
            "open_positions": 0,
            "sources": [],
            "series": [],
        }
    return strategies[strategy_id]


def _strategy_from_event(event: dict[str, Any], fallback: str, *, allow_reason: bool = False) -> str:
    explicit = _strategy_key(event, "")
    if explicit:
        return explicit
    reason = str(event.get("reason") or "").strip()
    if allow_reason and reason and not reason.startswith(("thesis_", "launcher_", "freshness_", "horizon", "stop", "target")):
        return reason
    return fallback


def _merge_strategy_ledger(strategies: dict[str, dict[str, Any]], source_id: str, ledger: list[dict[str, Any]]) -> set[str]:
    open_strategy_by_symbol: dict[str, str] = {}
    observed: set[str] = set()
    seen: set[tuple[str, str, str, str, str]] = set()
    for event in ledger:
        name = str(event.get("event") or "").lower()
        symbol = str(event.get("symbol") or "")
        key = (
            str(event.get("ts") or ""),
            name,
            symbol,
            str(event.get("side") or ""),
            str(event.get("decision_id") or event.get("reason") or event.get("pnl") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        if name == "entry":
            strategy_id = _strategy_from_event(event, source_id, allow_reason=True)
            if symbol:
                open_strategy_by_symbol[symbol] = strategy_id
            observed.add(strategy_id)
            row = _strategy_row(strategies, strategy_id)
            if source_id not in row["sources"]:
                row["sources"].append(source_id)
            continue
        if not ("exit" in name or name in {"manual_close", "flatten"}):
            continue
        pnl = _safe_float(event.get("pnl"))
        if pnl is None:
            continue
        strategy_id = _strategy_from_event(event, open_strategy_by_symbol.get(symbol, source_id), allow_reason=False)
        observed.add(strategy_id)
        row = _strategy_row(strategies, strategy_id)
        if source_id not in row["sources"]:
            row["sources"].append(source_id)
        row["realized_pnl"] = float(row.get("realized_pnl") or 0.0) + pnl
        row["closed_trades"] = int(row.get("closed_trades") or 0) + 1
        if pnl > 0:
            row["wins"] = int(row.get("wins") or 0) + 1
        elif pnl < 0:
            row["losses"] = int(row.get("losses") or 0) + 1
        if symbol:
            open_strategy_by_symbol.pop(symbol, None)
    return observed


def _merge_strategy_positions(strategies: dict[str, dict[str, Any]], source_id: str, positions: Any) -> None:
    if not isinstance(positions, dict):
        return
    for pos in positions.values():
        if not isinstance(pos, dict):
            continue
        row = _strategy_row(strategies, _strategy_key(pos, source_id))
        if source_id not in row["sources"]:
            row["sources"].append(source_id)
        row["open_positions"] = int(row.get("open_positions") or 0) + 1
        row["unrealized_pnl"] = float(row.get("unrealized_pnl") or 0.0) + float(_safe_float(pos.get("unrealized_pnl")) or 0.0)


def _merge_strategy_equity(strategies: dict[str, dict[str, Any]], source_id: str, equity: list[dict[str, Any]], state: dict[str, Any], strategy_ids: set[str]) -> None:
    points = equity[-240:]
    if not points and state.get("nav") is not None:
        points = [{"ts": state.get("updated_at") or datetime.now(timezone.utc).isoformat(), "nav": state.get("nav")}]
    if not points:
        return
    targets = sorted(strategy_ids)
    if not targets and state.get("strategy_id"):
        targets = [str(state.get("strategy_id"))]
    if not targets:
        targets = [source_id]
    base = _safe_float(points[0].get("nav")) or 0.0
    for strategy_id in targets:
        row = _strategy_row(strategies, strategy_id)
        if source_id not in row["sources"]:
            row["sources"].append(source_id)
        for point in points:
            nav = _safe_float(point.get("nav"))
            if nav is None:
                continue
            row["series"].append({"ts": point.get("ts") or point.get("timestamp"), "value": nav - base, "source": source_id})


def _safe_float(value: Any) -> float | None:
    try:
        out = float(value)
    except Exception:
        return None
    return out if math.isfinite(out) else None


def stop_strategy_source(payload: dict[str, Any]) -> dict[str, Any]:
    sources = payload.get("sources")
    if isinstance(sources, str):
        source_list = [sources]
    elif isinstance(sources, list):
        source_list = [str(item) for item in sources]
    else:
        source_list = [str(payload.get("source") or "")]
    stopped: dict[str, Any] = {}
    for source in source_list:
        if source == "paper_competition":
            stopped[source] = stop_c_auto_v2_micro_live("competition")
        elif source == "micro_live_personal":
            stopped[source] = stop_c_auto_v2_micro_live("personal")
        elif source == "legacy_paper":
            stopped[source] = stop_c_auto_v2_paper("personal")
        elif source == "monster_paper":
            stopped[source] = stop_monster_paper()
    if not stopped:
        raise ValueError("no stoppable strategy source provided")
    return {"ok": True, "stopped": stopped, "status": strategy_performance_status()}


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
    profile = okx_profile_for_environment(environment)
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
    if environment not in {"competition", "personal"}:
        raise ValueError("micro-live close is only enabled for competition or personal environment")
    profile = okx_profile_for_environment(environment)
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
    cancel_orders = _cancel_profile_symbol_open_orders(profile, inst_id)
    cancel_algos = _cancel_profile_symbol_algo_orders(profile, inst_id)
    close_result = _run_okx_json(["okx", "--profile", profile, "--json", "swap", "close", "--instId", inst_id, "--mgnMode", "isolated", "--posSide", "net", "--autoCxl"])
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


def close_account_symbol(payload: dict[str, Any]) -> dict[str, Any]:
    environment = str(payload.get("environment") or payload.get("env") or "").strip()
    if environment not in {"competition", "personal"}:
        raise ValueError("account close is only enabled for competition or personal environment")
    if not bool(payload.get("confirm_live_close")):
        raise ValueError("live close requires confirm_live_close=true")

    inst_id = str(payload.get("instId") or payload.get("inst_id") or "").strip().upper()
    symbol = str(payload.get("symbol") or "").strip().upper()
    if not inst_id and symbol:
        inst_id = _symbol_to_swap_inst_id(symbol)
    if not inst_id:
        raise ValueError("instId or symbol is required")

    profile = okx_profile_for_environment(environment)
    positions_resp = _run_okx_json(["okx", "--profile", profile, "--json", "account", "positions", "--instType", "SWAP"])
    if positions_resp["returncode"] != 0:
        raise RuntimeError(positions_resp["message"] or "unable to list positions")
    positions = _as_order_list(positions_resp["data"])
    position = next(
        (
            pos
            for pos in positions
            if str(pos.get("instId") or "").upper() == inst_id
            and abs(_number_float(pos.get("pos"))) > 0
        ),
        None,
    )
    if not position:
        cancel_orders = _cancel_profile_symbol_open_orders(profile, inst_id)
        cancel_algos = _cancel_profile_symbol_algo_orders(profile, inst_id)
        verification = wait_account_reconciliation(environment, symbols=[inst_id], require_flat=True)
        return {
            "ok": bool(verification.get("flat")) and bool(verification.get("orders_clean")),
            "closed": False,
            "environment": environment,
            "profile": profile,
            "instId": inst_id,
            "message": "symbol not open",
            "cancel_orders": cancel_orders,
            "cancel_algo_orders": cancel_algos,
            "verification": verification,
        }

    cancel_orders = _cancel_profile_symbol_open_orders(profile, inst_id)
    cancel_algos = _cancel_profile_symbol_algo_orders(profile, inst_id)
    mgn_mode = str(position.get("mgnMode") or payload.get("mgnMode") or "cross").strip() or "cross"
    close_result = _run_okx_json(
        [
            "okx",
            "--profile",
            profile,
            "--json",
            "swap",
            "close",
            "--instId",
            inst_id,
            "--mgnMode",
            mgn_mode,
            "--posSide",
            "net",
            "--autoCxl",
        ]
    )
    ok = close_result["returncode"] == 0
    verification = wait_account_reconciliation(environment, symbols=[inst_id], require_flat=True)
    result = {
        "ok": ok and bool(verification.get("flat")) and bool(verification.get("orders_clean")),
        "closed": ok,
        "environment": environment,
        "profile": profile,
        "instId": inst_id,
        "position": position,
        "mgnMode": mgn_mode,
        "cancel_orders": cancel_orders,
        "cancel_algo_orders": cancel_algos,
        "close_position": close_result,
        "message": close_result["message"],
        "verification": verification,
    }
    append_operation("account_close_symbol", environment, "accepted" if result["ok"] else "partial_error", result)
    if not ok:
        raise RuntimeError(close_result["message"] or f"close failed for {inst_id}")
    return result


def close_account_positions(payload: dict[str, Any]) -> dict[str, Any]:
    environment = str(payload.get("environment") or payload.get("env") or "").strip()
    if environment not in {"competition", "personal"}:
        raise ValueError("account close-all is only enabled for competition or personal environment")
    if not bool(payload.get("confirm_live_close")):
        raise ValueError("live close requires confirm_live_close=true")

    profile = okx_profile_for_environment(environment)
    positions_resp = _run_okx_json(["okx", "--profile", profile, "--json", "account", "positions", "--instType", "SWAP"])
    if positions_resp["returncode"] != 0:
        raise RuntimeError(positions_resp["message"] or "unable to list positions")
    positions = [
        pos
        for pos in _as_order_list(positions_resp["data"])
        if abs(_number_float(pos.get("pos"))) > 0
    ]
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    for pos in positions:
        inst_id = str(pos.get("instId") or "").strip()
        if not inst_id:
            errors.append(f"missing instId in position: {pos}")
            continue
        try:
            results.append(
                close_account_symbol(
                    {
                        "environment": environment,
                        "instId": inst_id,
                        "confirm_live_close": True,
                    }
                )
            )
        except Exception as exc:
            errors.append(f"{inst_id}: {exc}")

    order_cancel = cancel_all_open_swap_orders(environment)
    verification = wait_account_reconciliation(environment, require_flat=True)
    clean = bool(verification.get("flat")) and bool(verification.get("orders_clean"))
    result = {
        "ok": not errors and clean,
        "environment": environment,
        "profile": profile,
        "positions_found": len(positions),
        "positions_closed": sum(1 for item in results if item.get("closed")),
        "results": results,
        "errors": errors,
        "order_cancel": order_cancel,
        "verification": verification,
    }
    append_operation("account_close_all", environment, "accepted" if result["ok"] else "partial_error", result)
    return result


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
    assert_strategy_environment_exclusive("c_auto_v2_cross_section", environment)
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
            "--refresh-ohlcv",
            "--refresh-max-symbols",
            "150",
            "--max-positions",
            "15",
            "--paper-max-positions-per-strategy",
            "5",
            "--lookback-days",
            "240",
            "--max-train-rows",
            "250000",
            "--max-market-age-sec",
            "7200",
            "--min-fresh-symbols",
            "40",
            "--short-loss-cooldown-hours",
            "12",
            "--short-loss-lookback-hours",
            "24",
            "--short-loss-cooldown-min-losses",
            "2",
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
    if environment not in {"competition", "personal"}:
        append_operation("start_micro_live", environment, "rejected", {"reason": "unsupported_environment"})
        raise ValueError("micro-live 只允许比赛或个人真实账户环境")
    if not confirm:
        append_operation("start_micro_live", environment, "rejected", {"reason": "confirm_real_required"})
        raise ValueError("micro-live requires confirm_real=true")
    state_id = f"micro_live_{environment}"
    existing = find_c_auto_v2_micro_live_processes(state_id)
    if existing:
        append_operation("start_micro_live", environment, "already_running", {"processes": existing})
        return {"ok": True, "already_running": True, "processes": existing, "status": c_auto_v2_micro_live_status(state_id, environment)}
    assert_strategy_environment_exclusive("c_auto_v2_cross_section", environment)
    try:
        readiness = ensure_data_ready_for_start()
    except ValueError as exc:
        append_operation("start_micro_live", environment, "blocked", {"reason": str(exc)})
        raise
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
            "--okx-profile",
            okx_profile_for_environment(environment),
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
            "--refresh-ohlcv",
            "--refresh-max-symbols",
            "80",
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
            "3",
            "--max-leverage",
            "3",
            "--allow-aggressive-leverage",
            "--short-loss-cooldown-hours",
            "12",
            "--short-loss-lookback-hours",
            "24",
            "--short-loss-cooldown-min-losses",
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
    result.update(
        {
            "ok": True,
            "strategy": C_AUTO_V2_STRATEGY_ID,
            "environment": environment,
            "mode": "real",
            "source_mode": "micro_live",
            "okx_profile": okx_profile_for_environment(environment),
            "readiness": readiness,
        }
    )
    append_operation("start_micro_live", environment, "accepted", {"pid": result.get("pid"), "log_path": result.get("log_path")})
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


def stop_pro_paper(environment_filter: str | None = None) -> dict[str, Any]:
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    stopped: list[int] = []
    terminations: list[dict[str, Any]] = []
    stop_files: list[str] = []
    for proc in find_pro_paper_processes():
        strategy_id = proc.get("strategy_id") or "unknown"
        environment = proc.get("environment") or "personal"
        if environment_filter and environment != environment_filter:
            continue
        stop_path = CONTROL_DIR / f"pro_paper_{strategy_id}_{environment}.stop"
        stop_path.write_text(time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        stop_files.append(str(stop_path.relative_to(ROOT_DIR)))
        termination = terminate_process(proc.get("pid"))
        terminations.append(termination)
        if termination.get("terminated"):
            stopped.append(int(proc["pid"]))
    return {"ok": True, "environment": environment_filter, "stopped_pids": stopped, "stop_files": stop_files, "terminations": terminations}


def stop_research_sleeves(environment_filter: str | None = None) -> dict[str, Any]:
    stopped = []
    terminations = []
    stop_files = []
    for proc in find_research_sleeve_processes(environment=environment_filter):
        strategy_id = proc.get("strategy_id") or "unknown"
        environment = proc.get("environment") or "personal"
        state_id = str(strategy_id)
        stop_path = CONTROL_DIR / f"research_sleeve_{state_id}_{environment}.stop"
        stop_path.write_text(time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        stop_files.append(str(stop_path.relative_to(ROOT_DIR)))
        termination = terminate_process(proc.get("pid"))
        terminations.append(termination)
        if termination.get("terminated"):
            stopped.append(int(proc["pid"]))
    result = {"ok": True, "environment": environment_filter, "stopped_pids": stopped, "stop_files": stop_files, "terminations": terminations}
    append_operation("stop_research_sleeves", environment_filter, "accepted" if stopped else "already_stopped", result)
    return result


def stop_c_auto_v2_paper(environment_filter: str | None = None) -> dict[str, Any]:
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    stopped: list[int] = []
    terminations: list[dict[str, Any]] = []
    stop_files: list[str] = []
    flattened: list[str] = []
    for proc in find_c_auto_v2_paper_processes():
        state_id = proc.get("state_id") or "fixed1000_conservative"
        environment = proc.get("environment") or "personal"
        if environment_filter and environment != environment_filter:
            continue
        stop_path = CONTROL_DIR / f"c_auto_v2_paper_{state_id}_{environment}.stop"
        stop_path.write_text(time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        stop_files.append(str(stop_path.relative_to(ROOT_DIR)))
        flattened_path = flatten_c_auto_v2_paper_state(state_id, environment, "launcher_stop")
        if flattened_path:
            flattened.append(flattened_path)
        termination = terminate_process(proc.get("pid"))
        terminations.append(termination)
        if termination.get("terminated"):
            stopped.append(int(proc["pid"]))
    if environment_filter and not flattened:
        flattened_path = flatten_c_auto_v2_paper_state(C_AUTO_V2_STATE_ID, environment_filter, "launcher_stop")
        if flattened_path:
            flattened.append(flattened_path)
    return {"ok": True, "environment": environment_filter, "stopped_pids": stopped, "stop_files": stop_files, "flattened_state_files": flattened, "terminations": terminations}


def stop_c_auto_v2_micro_live(environment_filter: str | None = None) -> dict[str, Any]:
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    stopped: list[int] = []
    terminations: list[dict[str, Any]] = []
    stop_files: list[str] = []
    flattened: list[dict[str, Any]] = []
    for proc in find_c_auto_v2_micro_live_processes():
        state_id = proc.get("state_id") or "micro_live_competition"
        environment = proc.get("environment") or "competition"
        if environment_filter and environment != environment_filter:
            continue
        stop_path = CONTROL_DIR / f"c_auto_v2_micro_live_{state_id}_{environment}.stop"
        stop_path.write_text(time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        stop_files.append(str(stop_path.relative_to(ROOT_DIR)))
        flattened.append(flatten_c_auto_v2_micro_live_state(str(state_id), str(environment), "launcher_stop"))
        termination = terminate_process(proc.get("pid"))
        terminations.append(termination)
        if termination.get("terminated"):
            stopped.append(int(proc["pid"]))
    if environment_filter in {"competition", "personal"} and not flattened:
        flattened.append(flatten_c_auto_v2_micro_live_state(f"micro_live_{environment_filter}", environment_filter, "launcher_stop"))
    result = {"ok": True, "environment": environment_filter, "stopped_pids": stopped, "stop_files": stop_files, "flattened": flattened, "terminations": terminations}
    append_operation("stop_micro_live", environment_filter, "accepted" if stopped or flattened else "already_stopped", result)
    return result


def stop_monster_paper() -> dict[str, Any]:
    stopped: list[int] = []
    terminations: list[dict[str, Any]] = []
    for proc in [p for p in find_monster_processes() if "run_monster_paper.py" in p.get("command", "")]:
        termination = terminate_process(proc.get("pid"))
        terminations.append(termination)
        if termination.get("terminated"):
            stopped.append(int(proc["pid"]))
    return {"ok": True, "stopped_pids": stopped, "terminations": terminations}


def flatten_c_auto_v2_micro_live_state(state_id: str, environment: str, reason: str) -> dict[str, Any]:
    state_path = C_AUTO_V2_MICRO_LIVE_DIR / f"{state_id}_{environment}.json"
    state = read_json(state_path)
    if not isinstance(state, dict):
        return {"ok": False, "reason": "missing_state"}
    positions = dict(state.get("positions") or {})
    events: list[dict[str, Any]] = []
    profile = okx_profile_for_environment(environment)
    for symbol, pos in positions.items():
        inst_id = str(pos.get("inst_id") or _symbol_to_swap_inst_id(symbol))
        close_result = _run_okx_json(["okx", "--profile", profile, "--json", "swap", "close", "--instId", inst_id, "--mgnMode", "isolated", "--posSide", "net", "--autoCxl"])
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


def append_operation(action: str, environment: str | None, result: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "ts_bj": beijing_time(datetime.now(timezone.utc)),
        "action": action,
        "environment": environment,
        "result": result,
        "detail": json_safe(detail or {}),
    }
    with OPERATION_LOG_PATH.open("a") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return record


def operation_log_status(limit: int = 80) -> dict[str, Any]:
    records = iter_jsonl(OPERATION_LOG_PATH)
    return {"ok": True, "operations": records[-max(1, int(limit)) :], "updated_at": datetime.now(timezone.utc).isoformat()}


def refresh_ownership_reconciliation(environment: str, max_age_sec: float = 300.0) -> dict[str, Any]:
    if environment not in {"personal", "competition"}:
        return {}
    status_path = OWNERSHIP_DIR / environment / "reconciliation_status.json"
    if max_age_sec > 0 and status_path.exists():
        try:
            if time.time() - status_path.stat().st_mtime <= max_age_sec:
                return read_json(status_path) or {}
        except OSError:
            pass
    cmd = [
        PYTHON_BIN,
        "scripts/reconcile_live_ownership.py",
        "--environment",
        environment,
        "--write-status",
        "--json",
    ]
    try:
        proc = subprocess.run(cmd, cwd=ROOT_DIR, capture_output=True, text=True, timeout=75)
    except Exception as exc:
        append_operation("ownership_reconcile", environment, "error", {"error": str(exc)})
        return read_json(status_path) or {"ok": False, "errors": ["reconcile_process_failed"], "exchange_error": str(exc)}
    result = "ok" if proc.returncode == 0 else "blocked"
    detail = {"returncode": proc.returncode}
    if proc.returncode != 0:
        detail["stderr"] = (proc.stderr or "")[-1000:]
        detail["stdout"] = (proc.stdout or "")[-1000:]
    append_operation("ownership_reconcile", environment, result, detail)
    return read_json(status_path) or {}


def committee_decisions_status(limit: int = 160) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    sources = [
        ("competition", "paper_competition", C_AUTO_V2_MICRO_LIVE_DIR / "micro_live_competition_competition_ledger.jsonl"),
        ("personal", "micro_live_personal", C_AUTO_V2_MICRO_LIVE_DIR / "micro_live_personal_personal_ledger.jsonl"),
        ("personal", "legacy_paper", C_AUTO_V2_PAPER_DIR / "fixed1000_conservative_personal_ledger.jsonl"),
    ]
    for environment, source, path in sources:
        for event in iter_jsonl(path)[-600:]:
            event_type = str(event.get("event") or "")
            if event_type not in {"entry", "entry_rejected", "committee_note", "skip", "exit", "thesis_hold"}:
                continue
            rows.append(_committee_decision_row(environment, source, event))
    rows.sort(key=lambda item: str(item.get("ts") or ""), reverse=True)
    return {"ok": True, "updated_at": datetime.now(timezone.utc).isoformat(), "decisions": rows[: max(1, int(limit))]}


def _committee_decision_row(environment: str, source: str, event: dict[str, Any]) -> dict[str, Any]:
    event_type = str(event.get("event") or "")
    reason = str(event.get("reason") or "")
    accepted = event_type == "entry"
    rejected = event_type in {"entry_rejected", "skip", "committee_note"} and reason.startswith(("rejected", "committee_no", "freshness_", "micro_live_", "pretrade_"))
    thesis = event.get("thesis_contract") or (event.get("thesis") or {}).get("contract") if isinstance(event.get("thesis"), dict) else event.get("thesis_contract")
    return {
        "ts": event.get("ts"),
        "ts_bj": beijing_time(event.get("ts")),
        "environment": environment,
        "source": source,
        "event": event_type,
        "decision": "accepted" if accepted else "rejected" if rejected else event_type,
        "strategy_id": event.get("source_strategy_id") or event.get("strategy_id") or event.get("reason") if accepted else event.get("source_strategy_id") or event.get("strategy_id"),
        "symbol": event.get("symbol"),
        "side": event.get("side"),
        "reason": reason,
        "score": event.get("score"),
        "margin_usdt": event.get("margin_usdt"),
        "notional_usdt": event.get("notional_usdt"),
        "leverage": event.get("leverage"),
        "stop_price": event.get("stop_price"),
        "tp1_price": event.get("tp1_price"),
        "thesis_contract": thesis,
        "leverage_policy": event.get("leverage_policy"),
        "raw": event,
    }


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


def find_ownership_reconcile_processes() -> list[dict[str, Any]]:
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
        if not stripped or "scripts/run_ownership_reconcile_scheduler.py" not in stripped:
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


def find_us_equity_data_refresh_processes() -> list[dict[str, Any]]:
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
        if not stripped or "scripts/refresh_us_equity_data.py" not in stripped:
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
    heartbeat_age_sec = utc_age_seconds(status.get("heartbeat_at") or status.get("updated_at") or status.get("cycle_started_at"))
    status_running = str(status.get("scheduler_status") or "").lower() == "running" and heartbeat_age_sec is not None and heartbeat_age_sec <= 20 * 60
    us_equity_status_path = DATA_REFRESH_DIR / "us_equities_yfinance_status.json"
    us_equity_processes = find_us_equity_data_refresh_processes()
    us_equity_status = read_json(us_equity_status_path) or {}
    if not status and not processes:
        return {
            "available": bool(us_equity_processes or us_equity_status),
            "running": bool(us_equity_processes),
            "message": "no data refresh scheduler found",
            "us_equities": {
                "running": bool(us_equity_processes),
                "processes": us_equity_processes,
                "status": us_equity_status,
                "status_path": str(us_equity_status_path.relative_to(ROOT_DIR)) if us_equity_status_path.exists() else None,
            },
        }
    return {
        "available": True,
        "running": bool(processes) or status_running,
        "running_source": "process" if processes else ("fresh_status_heartbeat" if status_running else "none"),
        "heartbeat_age_sec": heartbeat_age_sec,
        "processes": processes,
        "status": status,
        "progress_tail": iter_jsonl(progress_path)[-20:],
        "status_path": str(status_path.relative_to(ROOT_DIR)) if status_path.exists() else None,
        "us_equities": {
            "running": bool(us_equity_processes),
            "processes": us_equity_processes,
            "status": us_equity_status,
            "status_path": str(us_equity_status_path.relative_to(ROOT_DIR)) if us_equity_status_path.exists() else None,
        },
    }


def data_readiness_status(min_symbols: int = START_READINESS_MIN_SYMBOLS) -> dict[str, Any]:
    refresh = data_refresh_status()
    records = iter_jsonl(DATA_REFRESH_DIR / "progress.jsonl")
    by_category: dict[str, dict[str, Any]] = {}
    for record in records:
        if str(record.get("status") or "") != "ok":
            continue
        kind = str(record.get("kind") or "ohlcv")
        timeframe = str(record.get("timeframe") or "")
        category_id = f"{kind}:{timeframe}" if timeframe else kind
        row = by_category.setdefault(
            category_id,
            {
                "category": category_id,
                "kind": kind,
                "timeframe": timeframe,
                "latest_data_ts": None,
                "latest_data_ts_bj": None,
                "symbols": set(),
                "target_symbols": {},
                "target_records": {},
                "ok_records": 0,
                "latest_record_at": None,
                "latest_record_at_bj": None,
            },
        )
        target = str(record.get("target_end") or record.get("cache_after") or "")
        if target:
            target_symbols = row.setdefault("target_symbols", {})
            if target not in target_symbols:
                target_symbols[target] = set()
            symbol = str(record.get("symbol") or "")
            if symbol:
                target_symbols[target].add(symbol)
            target_records = row.setdefault("target_records", {})
            target_records[target] = int(target_records.get(target) or 0) + 1
        if target and (not row["latest_data_ts"] or target > str(row["latest_data_ts"])):
            row["latest_data_ts"] = target
            row["latest_data_ts_bj"] = beijing_time(target)
            row["symbols"] = set()
            row["ok_records"] = 0
        if target and target == row["latest_data_ts"]:
            symbol = str(record.get("symbol") or "")
            if symbol:
                row["symbols"].add(symbol)
            row["ok_records"] = int(row.get("ok_records") or 0) + 1
        ts = str(record.get("ts") or "")
        if ts and (not row["latest_record_at"] or ts > str(row["latest_record_at"])):
            row["latest_record_at"] = ts
            row["latest_record_at_bj"] = beijing_time(ts)

    categories: list[dict[str, Any]] = []
    blocking: list[str] = []
    warnings: list[str] = []
    # Require latest fully closed OHLCV candles. The currently forming candle
    # should not block strategy startup.
    now_floor_1h = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    required_1h = (now_floor_1h - timedelta(hours=1)).isoformat().replace("+00:00", "+00:00")
    now_floor_4h = now_floor_1h.replace(hour=(now_floor_1h.hour // 4) * 4)
    required_4h = (now_floor_4h - timedelta(hours=4)).isoformat().replace("+00:00", "+00:00")
    required_ohlcv_targets = {"1h": required_1h, "4h": required_4h}
    required: set[str] = {f"ohlcv:{tf}" for tf in START_READINESS_TIMEFRAMES}
    required.update(f"{kind}:5m" for kind in START_READINESS_DERIVATIVE_KINDS)
    for category_id, row in sorted(by_category.items()):
        target_symbols = row.pop("target_symbols", {}) or {}
        target_records = row.pop("target_records", {}) or {}
        symbols = row.pop("symbols", set())
        if category_id in required:
            complete_targets = [
                str(target)
                for target, target_set in target_symbols.items()
                if len(target_set or set()) >= int(min_symbols)
            ]
            if complete_targets:
                selected_target = max(complete_targets)
                symbols = target_symbols.get(selected_target) or set()
                row["latest_data_ts"] = selected_target
                row["latest_data_ts_bj"] = beijing_time(selected_target)
                row["ok_records"] = int(target_records.get(selected_target) or len(symbols))
        symbol_count = len(symbols)
        row["symbol_count"] = symbol_count
        ready = True
        reasons: list[str] = []
        if category_id in required:
            if not bool(refresh.get("running")):
                ready = False
                reasons.append("data_refresh_not_running")
            if row.get("kind") == "ohlcv" and row.get("timeframe") in START_READINESS_TIMEFRAMES:
                timeframe = str(row.get("timeframe") or "")
                required_target = required_ohlcv_targets.get(timeframe)
                if required_target and str(row.get("latest_data_ts") or "") < required_target:
                    ready = False
                    reasons.append(f"latest_{timeframe}<{required_target}")
                if symbol_count < int(min_symbols):
                    ready = False
                    reasons.append(f"symbols<{int(min_symbols)}")
            elif row.get("kind") in START_READINESS_DERIVATIVE_KINDS:
                if symbol_count < int(min_symbols):
                    ready = False
                    reasons.append(f"symbols<{int(min_symbols)}")
        row["required_for_start"] = category_id in required
        row["ready"] = ready
        row["reasons"] = reasons
        if row["required_for_start"] and not ready:
            blocking.extend(f"{category_id}:{reason}" for reason in reasons)
        categories.append(row)

    for category_id in sorted(required):
        if category_id not in by_category:
            blocking.append(f"{category_id}:missing")
            kind, _, timeframe = category_id.partition(":")
            categories.append(
                {
                    "category": category_id,
                    "kind": kind,
                    "timeframe": timeframe,
                    "latest_data_ts": None,
                    "latest_data_ts_bj": None,
                    "symbol_count": 0,
                    "ok_records": 0,
                    "required_for_start": True,
                    "ready": False,
                    "reasons": ["missing"],
                }
            )

    status = refresh.get("status") if isinstance(refresh.get("status"), dict) else {}
    if status.get("failed"):
        warnings.append(f"data_refresh_failed_records:{status.get('failed')}")
    ready = bool(refresh.get("running")) and not blocking
    return {
        "ok": True,
        "ready": ready,
        "blocking_reasons": blocking,
        "warnings": warnings,
        "min_symbols": int(min_symbols),
        "required_1h_target": required_1h,
        "required_1h_target_bj": beijing_time(required_1h),
        "required_ohlcv_targets": required_ohlcv_targets,
        "data_refresh": refresh,
        "categories": sorted(categories, key=lambda item: (not bool(item.get("required_for_start")), str(item.get("category") or ""))),
        "log_tail": refresh.get("progress_tail") or [],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def ensure_data_ready_for_start() -> dict[str, Any]:
    refresh_start = start_data_refresh()
    readiness = data_readiness_status()
    readiness["data_refresh_start"] = refresh_start
    if not readiness.get("ready"):
        raise ValueError("数据还没有 ready，不能启动策略：" + "; ".join(readiness.get("blocking_reasons") or ["unknown"]))
    return readiness


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


def accounts_status() -> dict[str, Any]:
    return {
        "ok": True,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "accounts": {
            "personal": account_status("personal"),
            "competition": account_status("competition"),
        },
    }


def account_status(environment: str) -> dict[str, Any]:
    profile = okx_profile_for_environment(environment)
    balance_resp = _run_okx_json(["okx", "--profile", profile, "--json", "account", "balance"])
    positions_resp = _run_okx_json(["okx", "--profile", profile, "--json", "account", "positions", "--instType", "SWAP"])
    balance_data = balance_resp.get("data")
    positions_data = positions_resp.get("data")
    balance_view = _balance_view(balance_data)
    positions = _as_order_list(positions_data)
    open_positions = [pos for pos in positions if abs(_number_float(pos.get("pos"))) > 0]
    attribution = position_strategy_attribution(environment)
    return {
        "environment": environment,
        "profile": profile,
        "ok": balance_resp.get("returncode") == 0 and positions_resp.get("returncode") == 0,
        "balance_error": None if balance_resp.get("returncode") == 0 else balance_resp.get("message"),
        "positions_error": None if positions_resp.get("returncode") == 0 else positions_resp.get("message"),
        "balance": balance_view,
        "position_count": len(open_positions),
        "positions": [_position_view(pos, attribution=attribution) for pos in open_positions],
    }


def _balance_view(balance: Any) -> dict[str, Any]:
    item = balance[0] if isinstance(balance, list) and balance else balance if isinstance(balance, dict) else {}
    details = item.get("details", []) if isinstance(item, dict) else []
    usdt = next((row for row in details if row.get("ccy") == "USDT"), {}) if isinstance(details, list) else {}
    return {
        "total_eq": _number_float(item.get("totalEq")),
        "upl": _number_float(item.get("upl")),
        "imr": _number_float(item.get("imr")),
        "mmr": _number_float(item.get("mmr")),
        "usdt_eq": _number_float(usdt.get("eq")),
        "usdt_avail": _number_float(usdt.get("availEq") or usdt.get("cashBal")),
    }


def position_strategy_attribution(environment: str) -> dict[str, dict[str, Any]]:
    sources: list[tuple[str, Path]] = []
    if environment == "competition":
        sources.extend(
            [
                ("paper_competition", C_AUTO_V2_MICRO_LIVE_DIR / "micro_live_competition_competition_ledger.jsonl"),
                ("research_competition", RESEARCH_SLEEVES_DIR / "us_equity_token_equity_momentum_competition_ledger.jsonl"),
                ("research_competition", RESEARCH_SLEEVES_DIR / "us_equity_token_dislocation_reversion_competition_ledger.jsonl"),
                ("research_competition", RESEARCH_SLEEVES_DIR / "us_equity_token_okx_momentum_competition_ledger.jsonl"),
                ("research_competition", RESEARCH_SLEEVES_DIR / "btc_weekly_swing_3x_competition_ledger.jsonl"),
                ("research_competition", RESEARCH_SLEEVES_DIR / "btc_daily_breakout_swing_competition_ledger.jsonl"),
            ]
        )
    elif environment == "personal":
        sources.extend(
            [
                ("micro_live_personal", C_AUTO_V2_MICRO_LIVE_DIR / "micro_live_personal_personal_ledger.jsonl"),
                ("legacy_paper", C_AUTO_V2_PAPER_DIR / "fixed1000_conservative_personal_ledger.jsonl"),
            ]
        )

    by_key: dict[str, dict[str, Any]] = {}
    close_events = {"exit", "forced_exit", "manual_exit", "external_exit", "close", "closed"}
    for source, path in sources:
        for event in iter_jsonl(path)[-1500:]:
            symbol = event.get("symbol") or event.get("instId") or event.get("inst_id")
            if not symbol:
                continue
            event_type = str(event.get("event") or "")
            keys = _symbol_aliases(str(symbol))
            if event_type == "entry":
                strategy_id = _strategy_from_event(event, source, allow_reason=True)
                record = {
                    "strategy_id": strategy_id,
                    "strategy_display_name": _strategy_display_name(strategy_id),
                    "source": source,
                    "signal_family": event.get("signal_family") or event.get("reason"),
                    "entry_ts": event.get("ts"),
                    "entry_price": event.get("entry_price") or event.get("price") or event.get("exchange_fill_px"),
                    "side": event.get("side"),
                }
                for key in keys:
                    by_key[key] = record
            elif event_type in close_events:
                for key in keys:
                    by_key.pop(key, None)
    return by_key


def _strategy_display_name(strategy_id: str) -> str:
    for meta in KNOWN_STRATEGY_SLEEVES:
        if meta.get("strategy_id") == strategy_id:
            return str(meta.get("display_name") or strategy_id)
    if strategy_id == C_AUTO_V2_STRATEGY_ID:
        return "C-Auto fixed1000 conservative"
    return strategy_id.replace("_", " ").title()


def _symbol_aliases(symbol: str) -> set[str]:
    raw = symbol.strip().upper()
    base = raw
    if raw.endswith("-USDT-SWAP"):
        base = raw[: -len("-USDT-SWAP")]
    elif raw.endswith("-USDT"):
        base = raw[: -len("-USDT")]
    elif raw.endswith("_USDT"):
        base = raw[: -len("_USDT")]
    elif raw.endswith("/USDT"):
        base = raw[: -len("/USDT")]
    return {
        raw,
        base,
        f"{base}/USDT",
        f"{base}_USDT",
        f"{base}-USDT",
        f"{base}-USDT-SWAP",
    }


def _position_view(pos: dict[str, Any], attribution: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    contracts = _number_float(pos.get("pos"))
    inst_id = pos.get("instId") or pos.get("inst_id") or ""
    strategy = {}
    for key in _symbol_aliases(str(inst_id)):
        if attribution and key in attribution:
            strategy = attribution[key]
            break
    return {
        "instId": inst_id,
        "side": "long" if contracts > 0 else "short",
        "contracts": abs(contracts),
        "avgPx": _number_float(pos.get("avgPx")),
        "markPx": _number_float(pos.get("markPx")),
        "lever": pos.get("lever") or pos.get("leverage") or "",
        "marginMode": pos.get("mgnMode") or "",
        "upl": _number_float(pos.get("upl")),
        "notionalUsd": _number_float(pos.get("notionalUsd") or pos.get("notionalUsdPx")),
        "strategy_id": strategy.get("strategy_id") or "unknown",
        "strategy_display_name": strategy.get("strategy_display_name") or "未归因持仓",
        "strategy_source": strategy.get("source") or "",
        "signal_family": strategy.get("signal_family") or "",
        "entry_ts": strategy.get("entry_ts"),
        "entry_price": strategy.get("entry_price"),
    }


def _number_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except Exception:
        return default
    return out if math.isfinite(out) else default


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


def stop_c_auto_daily_review(environment_filter: str | None = None) -> dict[str, Any]:
    stopped: list[int] = []
    terminations: list[dict[str, Any]] = []
    for proc in find_c_auto_daily_review_processes():
        environment = proc.get("environment") or "personal"
        if environment_filter and environment != environment_filter:
            continue
        termination = terminate_process(proc.get("pid"))
        terminations.append(termination)
        if termination.get("terminated"):
            stopped.append(int(proc["pid"]))
    return {"ok": True, "environment": environment_filter, "stopped_pids": stopped, "terminations": terminations}


def start_data_refresh() -> dict[str, Any]:
    existing = find_data_refresh_processes()
    if existing:
        us_equity = start_us_equity_data_refresh()
        result = {"ok": True, "already_running": True, "processes": existing, "status": data_refresh_status(), "us_equities": us_equity}
        append_operation("start_data_refresh", None, "already_running", {"processes": existing})
        return result
    status = read_json(DATA_REFRESH_DIR / "status.json") or {}
    heartbeat_age_sec = utc_age_seconds(status.get("heartbeat_at") or status.get("updated_at") or status.get("cycle_started_at"))
    if str(status.get("scheduler_status") or "").lower() == "running" and heartbeat_age_sec is not None and heartbeat_age_sec <= 20 * 60:
        result = {
            "ok": True,
            "already_running": True,
            "processes": [],
            "running_source": "fresh_status_heartbeat",
            "heartbeat_age_sec": heartbeat_age_sec,
            "status": data_refresh_status(),
            "us_equities": {"ok": True, "skipped": True, "reason": "main_data_refresh_already_running"},
        }
        append_operation("start_data_refresh", None, "already_running", {"running_source": "fresh_status_heartbeat", "heartbeat_age_sec": heartbeat_age_sec})
        return result
    us_equity = start_us_equity_data_refresh()
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
    result.update({"ok": True, "service": "data_refresh", "us_equities": us_equity})
    append_operation("start_data_refresh", None, "accepted", {"pid": result.get("pid"), "log_path": result.get("log_path")})
    return result


def start_ownership_reconcile_scheduler() -> dict[str, Any]:
    existing = find_ownership_reconcile_processes()
    if existing:
        append_operation("start_ownership_reconcile", None, "already_running", {"processes": existing})
        return {"ok": True, "already_running": True, "processes": existing, "status": read_json(OWNERSHIP_DIR / "scheduler_status.json") or {}}
    result = run_script(
        [
            "python3",
            "scripts/run_ownership_reconcile_scheduler.py",
            "--environments",
            "personal,competition",
            "--interval-sec",
            "300",
        ],
        "ownership_reconcile",
    )
    result.update({"ok": True, "service": "ownership_reconcile"})
    append_operation("start_ownership_reconcile", None, "accepted", {"pid": result.get("pid"), "log_path": result.get("log_path")})
    return result


def start_us_equity_data_refresh() -> dict[str, Any]:
    existing = find_us_equity_data_refresh_processes()
    if existing:
        return {"ok": True, "already_running": True, "processes": existing}
    result = run_script(
        [
            "python3",
            "scripts/refresh_us_equity_data.py",
            "--symbols",
            "AMD,AMZN,ARM,COIN,CRCL,GOOGL,HOOD,INTC,MSTR,NVDA,PLTR,TSLA",
            "--period",
            "90d",
            "--interval-sec",
            "3600",
            "--loop",
        ],
        "us_equity_data_refresh",
    )
    result.update({"ok": True, "service": "us_equity_data_refresh"})
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
    terminations: list[dict[str, Any]] = []
    for proc in find_data_refresh_processes() + find_us_equity_data_refresh_processes():
        termination = terminate_process(proc.get("pid"))
        terminations.append(termination)
        if termination.get("terminated"):
            stopped.append(int(proc["pid"]))
    return {"ok": True, "stopped_pids": stopped, "terminations": terminations}


def stop_smartmoney_diffusion() -> dict[str, Any]:
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    stopped: list[int] = []
    terminations: list[dict[str, Any]] = []
    stop_path = CONTROL_DIR / "smartmoney_diffusion_collector.stop"
    stop_path.write_text(time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    for proc in find_smartmoney_diffusion_processes():
        termination = terminate_process(proc.get("pid"))
        terminations.append(termination)
        if termination.get("terminated"):
            stopped.append(int(proc["pid"]))
    return {"ok": True, "stopped_pids": stopped, "stop_file": str(stop_path.relative_to(ROOT_DIR)), "terminations": terminations}


def environment_process_snapshot(environment: str | None = None) -> list[dict[str, Any]]:
    processes: list[dict[str, Any]] = []
    for proc in find_pro_paper_processes():
        if environment and proc.get("environment") != environment:
            continue
        processes.append({"source": "pro_paper", **proc})
    for proc in find_research_sleeve_processes(environment=environment):
        processes.append({"source": "research_sleeve", **proc})
    for proc in find_c_auto_v2_paper_processes():
        if environment and proc.get("environment") != environment:
            continue
        processes.append({"source": "c_auto_v2_paper", **proc})
    for proc in find_c_auto_v2_micro_live_processes():
        if environment and proc.get("environment") != environment:
            continue
        processes.append({"source": "c_auto_v2_micro_live", **proc})
    for proc in find_c_auto_daily_review_processes():
        if environment and proc.get("environment") != environment:
            continue
        processes.append({"source": "c_auto_daily_review", **proc})
    return processes


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


def cancel_all_open_swap_orders(environment: str | None = None) -> dict[str, Any]:
    profiles = [okx_profile_for_environment(environment)] if environment else _okx_cancel_profiles()
    results = []
    for profile in profiles:
        open_orders = _cancel_profile_open_swap_orders(profile)
        algo_orders = _cancel_profile_open_swap_algo_orders(profile)
        results.append({"profile": profile, "open_orders": open_orders, "algo_orders": algo_orders})
    orders_failed = sum(int(item.get("open_orders", {}).get("orders_failed", 0) or 0) for item in results)
    algo_orders_failed = sum(int(item.get("algo_orders", {}).get("orders_failed", 0) or 0) for item in results)
    errors = [
        err
        for item in results
        for group in ("open_orders", "algo_orders")
        for err in item.get(group, {}).get("errors", [])
    ]
    return {
        "ok": orders_failed == 0 and algo_orders_failed == 0 and not errors,
        "environment": environment,
        "profiles": results,
        "orders_found": sum(int(item.get("open_orders", {}).get("orders_found", 0) or 0) for item in results),
        "orders_cancelled": sum(int(item.get("open_orders", {}).get("orders_cancelled", 0) or 0) for item in results),
        "orders_failed": orders_failed,
        "algo_orders_found": sum(int(item.get("algo_orders", {}).get("orders_found", 0) or 0) for item in results),
        "algo_orders_cancelled": sum(int(item.get("algo_orders", {}).get("orders_cancelled", 0) or 0) for item in results),
        "algo_orders_failed": algo_orders_failed,
        "errors": errors,
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


def _cancel_profile_open_swap_algo_orders(profile: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "profile": profile,
        "orders_found": 0,
        "orders_cancelled": 0,
        "orders_failed": 0,
        "errors": [],
        "cancelled": [],
    }
    orders = _list_open_swap_algo_orders(profile)
    if orders is None:
        result["errors"].append("unable to list open algo orders")
        return result
    result["orders_found"] = len(orders)
    for order in orders:
        inst_id = str(order.get("instId") or order.get("inst_id") or "").strip()
        algo_id = str(order.get("algoId") or order.get("algo_id") or "").strip()
        if not inst_id or not algo_id:
            result["orders_failed"] += 1
            result["errors"].append(f"missing instId/algoId in algo order: {order}")
            continue
        cancel = _run_okx_json(["okx", "--profile", profile, "--json", "swap", "algo", "cancel", "--instId", inst_id, "--algoId", algo_id])
        if cancel["returncode"] == 0:
            result["orders_cancelled"] += 1
            result["cancelled"].append({"instId": inst_id, "algoId": algo_id})
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


def _list_open_swap_algo_orders(profile: str) -> list[dict[str, Any]] | None:
    direct = _run_okx_json(["okx", "--profile", profile, "--json", "swap", "algo", "orders"])
    if direct["returncode"] == 0:
        return _as_order_list(direct["data"])

    orders: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    had_success = False
    candidates = _candidate_swap_inst_ids(profile)[:20]
    for inst_id in candidates:
        resp = _run_okx_json(["okx", "--profile", profile, "--json", "swap", "algo", "orders", "--instId", inst_id])
        if resp["returncode"] != 0:
            continue
        had_success = True
        for order in _as_order_list(resp["data"]):
            algo_id = str(order.get("algoId") or order.get("algo_id") or "")
            key = (str(order.get("instId") or inst_id), algo_id)
            if algo_id and key not in seen:
                seen.add(key)
                if not order.get("instId"):
                    order["instId"] = inst_id
                orders.append(order)
    if orders:
        return orders
    return [] if had_success else None


def account_reconciliation_snapshot(environment: str | None, symbols: list[str] | None = None) -> dict[str, Any]:
    profiles = [okx_profile_for_environment(environment)] if environment else _okx_cancel_profiles()
    symbol_filter = {item.upper() for item in (symbols or []) if item}
    profile_results = [_profile_account_reconciliation(profile, symbol_filter) for profile in profiles]
    position_count = sum(int(item.get("position_count", 0) or 0) for item in profile_results)
    order_count = sum(int(item.get("open_order_count", 0) or 0) for item in profile_results)
    algo_count = sum(int(item.get("algo_order_count", 0) or 0) for item in profile_results)
    errors = [err for item in profile_results for err in item.get("errors", [])]
    return {
        "ok": not errors,
        "environment": environment,
        "profiles": profile_results,
        "position_count": position_count,
        "open_order_count": order_count,
        "algo_order_count": algo_count,
        "flat": position_count == 0 and not errors,
        "orders_clean": order_count == 0 and algo_count == 0 and not errors,
        "errors": errors,
    }


def wait_account_reconciliation(
    environment: str | None,
    symbols: list[str] | None = None,
    require_flat: bool = False,
    attempts: int = 4,
    sleep_sec: float = 0.75,
) -> dict[str, Any]:
    latest: dict[str, Any] = {}
    for attempt in range(max(attempts, 1)):
        latest = account_reconciliation_snapshot(environment, symbols=symbols)
        orders_clean = bool(latest.get("orders_clean"))
        flat_ok = (not require_flat) or bool(latest.get("flat"))
        if orders_clean and flat_ok:
            break
        if attempt + 1 < attempts:
            time.sleep(sleep_sec)
    return latest


def _profile_account_reconciliation(profile: str, symbol_filter: set[str]) -> dict[str, Any]:
    errors: list[str] = []
    positions_resp = _run_okx_json(["okx", "--profile", profile, "--json", "account", "positions", "--instType", "SWAP"])
    if positions_resp["returncode"] == 0:
        positions = [
            pos
            for pos in _as_order_list(positions_resp["data"])
            if abs(_number_float(pos.get("pos"))) > 0
            and (not symbol_filter or str(pos.get("instId") or "").upper() in symbol_filter)
        ]
    else:
        positions = []
        errors.append(f"positions: {positions_resp['message']}")

    open_orders = _list_open_swap_orders(profile)
    if open_orders is None:
        open_orders = []
        errors.append("open_orders: unable to list")
    if symbol_filter:
        open_orders = [order for order in open_orders if str(order.get("instId") or "").upper() in symbol_filter]

    algo_orders = _list_open_swap_algo_orders(profile)
    if algo_orders is None:
        algo_orders = []
        errors.append("algo_orders: unable to list")
    if symbol_filter:
        algo_orders = [order for order in algo_orders if str(order.get("instId") or "").upper() in symbol_filter]

    return {
        "profile": profile,
        "position_count": len(positions),
        "open_order_count": len(open_orders),
        "algo_order_count": len(algo_orders),
        "positions": positions,
        "open_orders": open_orders,
        "algo_orders": algo_orders,
        "errors": errors,
    }


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
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT_DIR),
            env=okx_command_env(command_profile(cmd)),
            capture_output=True,
            text=True,
            timeout=20,
        )
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
    env["OKX_ENVIRONMENT_RUNNER"] = "1"
    profile = command_profile(cmd)
    if profile == "live":
        env["LIVE_TRADING"] = "true"
    if profile and profile != "live":
        for key in OKX_ENV_CREDENTIAL_KEYS:
            env.pop(key, None)
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
        body = json.dumps(with_display_times(json_safe(payload)), ensure_ascii=False, default=str, allow_nan=False).encode("utf-8")
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
                params = parse_qs(parsed.query)
                env = str((params.get("env") or [""])[0]).strip() or None
                self.send_json(200, self.status_payload(env if env in ALLOWED_ENVS else None))
                return
            if path == "/api/launch-options":
                params = parse_qs(parsed.query)
                env = str((params.get("env") or ["personal"])[0]).strip()
                runtime_env = env if env in {"personal", "competition"} else "personal"
                self.send_json(
                    200,
                    {
                        "ok": True,
                        "strategies": strategy_options(),
                        "runtime_plan": environment_runtime_plan(runtime_env),
                        "environments": [
                            {"id": "personal", "label": "个人"},
                            {"id": "demo", "label": "Demo"},
                            {"id": "competition", "label": "比赛"},
                        ],
                        "modes": [
                            {"id": "paper", "label": "Paper=比赛小额"},
                            {"id": "real", "label": "真实交易"},
                        ],
                        "primary_strategy_id": C_AUTO_V2_STRATEGY_ID,
                    },
                )
                return
            if path == "/api/pro-paper":
                params = parse_qs(parsed.query)
                env = str((params.get("env") or ["personal"])[0]).strip()
                self.send_json(200, {"ok": True, **pro_paper_status(environment=env if env in ALLOWED_ENVS else "personal")})
                return
            if path == "/api/c-auto-v2-paper":
                self.send_json(200, {"ok": True, **c_auto_v2_paper_status(environment="competition")})
                return
            if path == "/api/c-auto-v2-micro-live":
                params = parse_qs(parsed.query)
                env = str((params.get("env") or [""])[0]).strip() or None
                state_id = f"micro_live_{env}" if env in {"competition", "personal"} else None
                runtime_env = env if env in ALLOWED_ENVS else None
                payload = {"ok": True, **c_auto_v2_micro_live_status(state_id=state_id, environment=runtime_env)}
                env_processes = environment_process_snapshot(runtime_env)
                payload["environment_processes"] = env_processes
                payload["environment_process_count"] = len(env_processes)
                payload["running"] = bool(payload.get("running") or env_processes)
                self.send_json(200, payload)
                return
            if path == "/api/strategy-performance":
                self.send_json(200, strategy_performance_status())
                return
            if path == "/api/committee-decisions":
                self.send_json(200, committee_decisions_status())
                return
            if path == "/api/accounts":
                self.send_json(200, accounts_status())
                return
            if path == "/api/account/reconcile":
                params = parse_qs(parsed.query)
                env = str((params.get("env") or params.get("environment") or [""])[0]).strip()
                environment = env if env in {"competition", "personal"} else None
                self.send_json(200, account_reconciliation_snapshot(environment))
                return
            if path == "/api/operations":
                self.send_json(200, operation_log_status())
                return
            if path == "/api/8-layer-pipeline":
                self.send_json(200, eight_layer_pipeline_status())
                return
            if path == "/api/data-refresh":
                self.send_json(200, {"ok": True, **data_refresh_status()})
                return
            if path == "/api/data-readiness":
                self.send_json(200, data_readiness_status())
                return
            if path == "/api/runtime-status":
                params = parse_qs(parsed.query)
                env = str((params.get("env") or params.get("environment") or [""])[0]).strip()
                if env not in {"personal", "competition"}:
                    raise ValueError("runtime-status requires env=personal or env=competition")
                refresh_ownership_reconciliation(env)
                self.send_json(200, EnvironmentRunner().status(env))
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
            if path in {"/api/stop", "/api/environment-stop"}:
                payload = self.read_json_body()
                self.send_json(200, self.handle_stop(payload))
                return
            if path == "/api/restart":
                payload = self.read_json_body()
                self.handle_stop(payload)
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
            if path == "/api/data-refresh-start":
                self.send_json(200, start_data_refresh())
                return
            if path == "/api/ownership-reconcile-start":
                self.send_json(200, start_ownership_reconcile_scheduler())
                return
            if path in {"/api/environment-start", "/api/c-auto-v2-micro-live-start"}:
                payload = self.read_json_body()
                environment = str(payload.get("environment") or payload.get("env") or "competition")
                confirm = bool(payload.get("confirm_real")) and (environment != "competition" or bool(payload.get("confirm_competition")))
                result = start_environment_strategies(environment, confirm=confirm)
                result["mode"] = "environment_runner"
                if path == "/api/c-auto-v2-micro-live-start":
                    result["deprecated_alias"] = "/api/environment-start"
                result["data_refresh"] = data_refresh_status()
                result["smartmoney_diffusion"] = smartmoney_diffusion_status()
                result["daily_review"] = c_auto_daily_review_status()
                self.send_json(200, result)
                return
            if path == "/api/c-auto-v2-paper-start":
                payload = self.read_json_body()
                if not bool(payload.get("confirm_competition")):
                    raise ValueError("Paper Trade 现在使用比赛账户小额实盘，需要确认比赛环境")
                result = start_environment_strategies("competition", confirm=True)
                result["paper_alias"] = True
                result["mode"] = "environment_runner"
                result["data_refresh"] = data_refresh_status()
                result["smartmoney_diffusion"] = smartmoney_diffusion_status()
                result["daily_review"] = c_auto_daily_review_status()
                self.send_json(200, result)
                return
            if path == "/api/strategy-stop":
                payload = self.read_json_body()
                self.send_json(200, stop_strategy_source(payload))
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
            if path == "/api/account/close-symbol":
                payload = self.read_json_body()
                self.send_json(200, close_account_symbol(payload))
                return
            if path == "/api/account/close-all":
                payload = self.read_json_body()
                self.send_json(200, close_account_positions(payload))
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
                if not confirm_competition:
                    raise ValueError("Paper Trade 现在使用比赛账户小额实盘，需要确认比赛环境")
                paper_env = "competition"
                daily_review = c_auto_daily_review_status()
                result = start_environment_strategies(paper_env, confirm=True)
                result["paper_alias"] = True
                result["data_refresh"] = data_refresh
                result["smartmoney_diffusion"] = smartmoney_diffusion
                result["daily_review"] = daily_review
                result["kill_switch"] = kill_switch
                return result
            daily_review = c_auto_daily_review_status()
            paper = {"ok": True, "skipped": True, "reason": "environment runner owns strategy startup"}
            micro_live_confirmed = confirm_real and (env != "competition" or confirm_competition)
            micro_live = start_environment_strategies(env, confirm=micro_live_confirmed)
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

        if option["kind"] == "legacy":
            raise ValueError("legacy 策略已退役，不能通过 launcher 启动；请使用 Strategy Office + EnvironmentRunner")

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

    def handle_stop(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        env = str(payload.get("env") or payload.get("environment") or "").strip()
        environment = env if env in ALLOWED_ENVS else None

        if environment:
            result = {"ok": True, "mode": "environment_stop", "environment": environment}
            kill_switch = {"active": False, "environment": environment, "reason": "not set for environment-scoped stop"}
            pro = stop_pro_paper(environment)
            research_sleeves = stop_research_sleeves(environment)
            c_auto_v2 = stop_c_auto_v2_paper(environment)
            c_auto_v2_micro_live = stop_c_auto_v2_micro_live(environment)
            data_refresh = {"ok": True, "skipped": True, "reason": "shared service not stopped by environment-scoped stop"}
            smartmoney_diffusion = {"ok": True, "skipped": True, "reason": "shared service not stopped by environment-scoped stop"}
            daily_review = stop_c_auto_daily_review(environment)
            order_cancel = cancel_all_open_swap_orders(environment)
        else:
            kill_switch = activate_kill_switch("launcher pause all")
            result = run_script([str(ROOT_DIR / "manage_local.sh"), "stop"], "stop")
            pro = stop_pro_paper()
            research_sleeves = stop_research_sleeves()
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
                "research_sleeves": research_sleeves,
                "c_auto_v2_paper": c_auto_v2,
                "c_auto_v2_micro_live": c_auto_v2_micro_live,
                "data_refresh": data_refresh,
                "smartmoney_diffusion": smartmoney_diffusion,
                "daily_review": daily_review,
            }
        )
        remaining = environment_process_snapshot(environment)
        account_verification = wait_account_reconciliation(environment, require_flat=False)
        process_clean = len(remaining) == 0
        orders_clean = bool(account_verification.get("orders_clean"))
        result["verification"] = {
            "environment": environment,
            "running_processes": len(remaining),
            "processes": remaining,
            "process_clean": process_clean,
            "orders_clean": orders_clean,
            "account": account_verification,
            "ok": process_clean and orders_clean,
        }
        result["ok"] = bool(result["verification"]["ok"])
        append_operation(
            "environment_stop" if environment else "stop_all",
            environment,
            "accepted" if result["verification"]["ok"] else "partial_error",
            {
                "running_processes": len(remaining),
                "processes": remaining,
                "order_cancel": order_cancel,
                "account_verification": account_verification,
            },
        )
        return result

    def reset_paper_state(self, payload: dict[str, Any]) -> None:
        strategy = str(payload.get("strategy", "")).strip()
        mode = str(payload.get("mode", "paper")).strip()
        env = str(payload.get("env", "personal")).strip()
        if mode != "paper" or strategy != C_AUTO_V2_STRATEGY_ID:
            return
        archive_c_auto_v2_paper_session(C_AUTO_V2_STATE_ID, env, "launcher_restart")

    def status_payload(self, environment: str | None = None) -> dict[str, Any]:
        pids = pid_snapshot()
        paper_env = environment if environment in ALLOWED_ENVS else None
        micro_live_env = environment if environment in {"competition", "personal"} else None
        micro_live_state = f"micro_live_{micro_live_env}" if micro_live_env else None
        return {
            "ok": True,
            "root": str(ROOT_DIR),
            "default_dashboard_port": DEFAULT_DASHBOARD_PORT,
            "default_dashboard_url": f"http://127.0.0.1:{DEFAULT_DASHBOARD_PORT}/",
            "default_yolo_url": f"http://127.0.0.1:{DEFAULT_DASHBOARD_PORT}/yolo",
            "summary": active_summary(),
            "pids": pids,
            "environment": environment,
            "pro_paper": pro_paper_status(environment=paper_env or "personal"),
            "c_auto_v2_paper": c_auto_v2_paper_status(environment=paper_env),
            "c_auto_v2_micro_live": c_auto_v2_micro_live_status(state_id=micro_live_state, environment=micro_live_env),
            "data_refresh": data_refresh_status(),
            "smartmoney_diffusion": smartmoney_diffusion_status(),
            "daily_review": c_auto_daily_review_status(),
            "kill_switch": kill_switch_status(),
            "runtime_plan": environment_runtime_plan(environment) if environment in {"personal", "competition"} else {
                "personal": environment_runtime_plan("personal"),
                "competition": environment_runtime_plan("competition"),
            },
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
