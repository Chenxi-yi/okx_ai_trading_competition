#!/usr/bin/env python3
"""Long-running system watchdog for the unified OKX trading runtime.

The watchdog owns no trading logic. It only keeps the shared control plane
alive: data refresh, ownership reconciliation, and registry-driven environment
runners.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
ENGINE_DIR = ROOT / "engine"
LOG_DIR = ENGINE_DIR / "logs" / "system_watchdog"
CONTROL_DIR = ENGINE_DIR / "control"
STATUS_PATH = LOG_DIR / "status.json"
EVENTS_PATH = LOG_DIR / "events.jsonl"
PID_PATH = CONTROL_DIR / "system_watchdog.pid"
STOP_PATH = CONTROL_DIR / "system_watchdog.stop"
WINDOWS_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

sys.path.insert(0, str(ROOT / "launcher"))
sys.path.insert(0, str(ENGINE_DIR))

try:
    import launcher_server as launcher
    from runtime import EnvironmentRunner
except Exception as exc:  # pragma: no cover - surfaced in main status.
    launcher = None  # type: ignore[assignment]
    EnvironmentRunner = None  # type: ignore[assignment]
    IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
else:
    IMPORT_ERROR = ""


def main() -> int:
    args = parse_args()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    existing_pid = read_pid(PID_PATH)
    if existing_pid and existing_pid != os.getpid() and pid_alive(existing_pid):
        write_json(
            STATUS_PATH,
            {
                "ok": True,
                "status": "already_running",
                "pid": existing_pid,
                "attempted_pid": os.getpid(),
                "updated_at": utc_now(),
            },
        )
        return 0
    PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
    if STOP_PATH.exists():
        STOP_PATH.unlink()
    cycles = 0
    while True:
        cycles += 1
        status = run_cycle(args, cycles)
        write_json(STATUS_PATH, status)
        append_jsonl(EVENTS_PATH, compact_event(status))
        if not args.loop or STOP_PATH.exists():
            break
        time.sleep(max(5.0, float(args.interval_sec)))
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Keep unified trading runtime healthy for long unattended runs")
    p.add_argument("--loop", action="store_true")
    p.add_argument("--interval-sec", type=float, default=60.0)
    p.add_argument("--environments", default="personal,competition")
    p.add_argument("--max-runner-rss-mb", type=float, default=1400.0)
    p.add_argument("--max-service-rss-mb", type=float, default=900.0)
    p.add_argument("--max-launcher-rss-mb", type=float, default=1200.0)
    p.add_argument("--launcher-port", default=os.environ.get("OKX_TRADING_SYSTEM_PORT", "8788"))
    p.add_argument("--keep-launcher-alive", action="store_true")
    p.add_argument("--stale-grace-sec", type=float, default=300.0)
    p.add_argument("--log-keep-days", type=float, default=21.0)
    return p.parse_args()


def run_cycle(args: argparse.Namespace, cycles: int) -> dict[str, Any]:
    started_at = utc_now()
    actions: list[dict[str, Any]] = []
    errors: list[str] = []
    if IMPORT_ERROR or launcher is None or EnvironmentRunner is None:
        errors.append(f"import_error: {IMPORT_ERROR}")
        return base_status(args, cycles, started_at, actions, errors)

    cleanup_old_logs(float(args.log_keep_days), actions, errors)
    launcher_status = ensure_launcher(args, actions, errors) if args.keep_launcher_alive else {"ok": True, "managed": False}
    data_refresh = call("start_data_refresh", launcher.start_data_refresh, actions, errors)
    ownership = call("start_ownership_reconcile", launcher.start_ownership_reconcile_scheduler, actions, errors)

    runner = EnvironmentRunner(root=ROOT)
    environment_rows: dict[str, Any] = {}
    for environment in selected_environments(args.environments):
        env_status = call(f"runner_status:{environment}", runner.status, actions, errors, environment)
        if isinstance(env_status, dict):
            environment_rows[environment] = env_status
            restart_needed = restart_stale_or_huge_processes(
                runner,
                env_status,
                max_runner_rss_mb=float(args.max_runner_rss_mb),
                stale_grace_sec=float(args.stale_grace_sec),
                actions=actions,
                errors=errors,
            )
            if restart_needed or not env_status.get("ok"):
                call(f"start_environment:{environment}", launcher.start_environment_strategies, actions, errors, environment, True)

    restart_huge_service_processes(
        service_result=data_refresh,
        label="data_refresh",
        max_rss_mb=float(args.max_service_rss_mb),
        actions=actions,
        errors=errors,
    )
    restart_huge_service_processes(
        service_result=ownership,
        label="ownership_reconcile",
        max_rss_mb=float(args.max_service_rss_mb),
        actions=actions,
        errors=errors,
    )

    return {
        **base_status(args, cycles, started_at, actions, errors),
        "data_refresh": summarize_result(data_refresh),
        "launcher": launcher_status,
        "ownership_reconcile": summarize_result(ownership),
        "environments": environment_rows,
    }


def base_status(args: argparse.Namespace, cycles: int, started_at: str, actions: list[dict[str, Any]], errors: list[str]) -> dict[str, Any]:
    return {
        "ok": not errors,
        "status": "running",
        "pid": os.getpid(),
        "cycles": cycles,
        "started_at": started_at,
        "updated_at": utc_now(),
        "interval_sec": float(args.interval_sec),
        "actions": actions[-20:],
        "errors": errors[-20:],
    }


def call(name: str, fn: Callable[..., Any], actions: list[dict[str, Any]], errors: list[str], *args: Any) -> Any:
    try:
        result = fn(*args)
        actions.append({"ts": utc_now(), "action": name, "ok": True, "summary": summarize_result(result)})
        return result
    except Exception as exc:
        message = f"{name}: {type(exc).__name__}: {exc}"
        errors.append(message)
        actions.append({"ts": utc_now(), "action": name, "ok": False, "error": message})
        return {"ok": False, "error": message}


def restart_stale_or_huge_processes(
    runner: Any,
    env_status: dict[str, Any],
    *,
    max_runner_rss_mb: float,
    stale_grace_sec: float,
    actions: list[dict[str, Any]],
    errors: list[str],
) -> bool:
    restart_needed = False
    for row in env_status.get("strategies") or []:
        if not isinstance(row, dict):
            continue
        scheduler = row.get("scheduler") if isinstance(row.get("scheduler"), dict) else {}
        scheduler_stale = bool(scheduler.get("stale_without_process")) or (
            scheduler.get("fresh") is False and float(scheduler.get("age_sec") or 0.0) > stale_grace_sec
        )
        processes = row.get("processes") if isinstance(row.get("processes"), list) else []
        if not processes or scheduler_stale:
            restart_needed = True
        for proc in processes:
            if not isinstance(proc, dict):
                continue
            pid = safe_int(proc.get("pid"))
            if pid <= 0:
                continue
            rss_mb = process_rss_mb(pid)
            if rss_mb is not None:
                proc["rss_mb"] = round(rss_mb, 2)
            if max_runner_rss_mb > 0 and rss_mb is not None and rss_mb > max_runner_rss_mb:
                terminate_process(pid, actions, errors, reason=f"runner_rss_mb>{max_runner_rss_mb:g}", runner=runner)
                restart_needed = True
            elif scheduler_stale:
                terminate_process(pid, actions, errors, reason="scheduler_stale", runner=runner)
    return restart_needed


def restart_huge_service_processes(
    *,
    service_result: Any,
    label: str,
    max_rss_mb: float,
    actions: list[dict[str, Any]],
    errors: list[str],
) -> None:
    if max_rss_mb <= 0 or not isinstance(service_result, dict):
        return
    for proc in service_result.get("processes") or []:
        if not isinstance(proc, dict):
            continue
        pid = safe_int(proc.get("pid"))
        if pid <= 0:
            continue
        rss_mb = process_rss_mb(pid)
        if rss_mb is not None and rss_mb > max_rss_mb:
            terminate_process(pid, actions, errors, reason=f"{label}_rss_mb>{max_rss_mb:g}", runner=None)


def ensure_launcher(args: argparse.Namespace, actions: list[dict[str, Any]], errors: list[str]) -> dict[str, Any]:
    processes = find_launcher_processes()
    max_rss_mb = float(args.max_launcher_rss_mb)
    port = str(args.launcher_port or "8788")
    for proc in list(processes):
        pid = safe_int(proc.get("pid"))
        rss_mb = process_rss_mb(pid) if pid > 0 else None
        proc["rss_mb"] = round(rss_mb, 2) if rss_mb is not None else None
        if max_rss_mb > 0 and rss_mb is not None and rss_mb > max_rss_mb:
            terminate_process(pid, actions, errors, reason=f"launcher_rss_mb>{max_rss_mb:g}", runner=None)
            processes = [item for item in processes if safe_int(item.get("pid")) != pid]
    alive = [item for item in processes if pid_alive(safe_int(item.get("pid")))]
    if alive:
        return {"ok": True, "already_running": True, "processes": alive}
    result = start_launcher(port)
    actions.append({"ts": utc_now(), "action": "start_launcher", "ok": bool(result.get("ok")), "summary": result})
    return result


def find_launcher_processes() -> list[dict[str, Any]]:
    try:
        import psutil  # type: ignore

        rows: list[dict[str, Any]] = []
        for proc in psutil.process_iter(["pid", "cmdline"]):
            pid = int(proc.info.get("pid") or 0)
            if pid == os.getpid():
                continue
            command = " ".join(str(part) for part in (proc.info.get("cmdline") or []))
            if "launcher/launcher_server.py" in command or "launcher\\launcher_server.py" in command:
                rows.append({"pid": pid, "command": command, "source": "psutil"})
        return rows
    except Exception:
        pass
    if os.name == "nt":
        script = (
            "$rows = Get-CimInstance Win32_Process | "
            "Where-Object { $_.CommandLine -match 'launcher[/\\\\]launcher_server\\.py' }; "
            "$rows | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
        )
        try:
            proc = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", script],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                timeout=8,
                creationflags=WINDOWS_NO_WINDOW,
            )
            payload = json.loads(proc.stdout.strip() or "[]")
        except Exception:
            return []
        if isinstance(payload, dict):
            payload = [payload]
        return [
            {"pid": int(item.get("ProcessId")), "command": str(item.get("CommandLine") or "")}
            for item in payload if isinstance(item, dict) and int(item.get("ProcessId") or 0) != os.getpid()
        ]
    try:
        proc = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=5)
    except Exception:
        return []
    rows = []
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if not stripped or "launcher/launcher_server.py" not in stripped:
            continue
        try:
            pid_raw, command = stripped.split(None, 1)
            pid = int(pid_raw)
        except Exception:
            continue
        if pid != os.getpid():
            rows.append({"pid": pid, "command": command})
    return rows


def start_launcher(port: str) -> dict[str, Any]:
    python_bin = os.environ.get("OKX_TRADING_SYSTEM_PYTHON") or sys.executable or "python"
    log_dir = ENGINE_DIR / "logs"
    control_dir = ENGINE_DIR / "control"
    log_dir.mkdir(parents=True, exist_ok=True)
    control_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_log = log_dir / f"launcher_watchdog_{stamp}.out.log"
    err_log = log_dir / f"launcher_watchdog_{stamp}.err.log"
    env = os.environ.copy()
    env["OKX_TRADING_SYSTEM_PYTHON"] = python_bin
    if os.name == "nt":
        appdata = env.get("APPDATA")
        path_parts = []
        if appdata:
            path_parts.append(str(Path(appdata) / "npm"))
        path_parts.append(r"C:\Program Files\nodejs")
        if env.get("PATH"):
            path_parts.append(env["PATH"])
        env["PATH"] = os.pathsep.join(dict.fromkeys(path_parts))
    popen_kwargs: dict[str, Any] = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS | WINDOWS_NO_WINDOW
    with out_log.open("ab") as out_fh, err_log.open("ab") as err_fh:
        proc = subprocess.Popen(
            [python_bin, "launcher/launcher_server.py", "--port", str(port)],
            cwd=str(ROOT),
            stdout=out_fh,
            stderr=err_fh,
            env=env,
            **popen_kwargs,
        )
    (control_dir / "launcher.pid").write_text(str(proc.pid), encoding="utf-8")
    return {"ok": True, "pid": proc.pid, "out_log": str(out_log.relative_to(ROOT)), "err_log": str(err_log.relative_to(ROOT))}


def terminate_process(pid: int, actions: list[dict[str, Any]], errors: list[str], *, reason: str, runner: Any | None) -> None:
    try:
        if runner is not None:
            result = runner._terminate_process(pid)  # EnvironmentRunner owns cross-platform process termination.
        else:
            result = terminate_process_tree(pid)
        actions.append({"ts": utc_now(), "action": "terminate_process", "reason": reason, "pid": pid, "result": result})
    except Exception as exc:
        errors.append(f"terminate_process:{pid}: {type(exc).__name__}: {exc}")


def terminate_process_tree(pid: int, *, grace_sec: float = 3.0) -> dict[str, Any]:
    result: dict[str, Any] = {"pid": pid, "terminated": False, "method": "psutil"}
    try:
        import psutil  # type: ignore

        proc = psutil.Process(int(pid))
        children = proc.children(recursive=True)
        for child in children:
            child.terminate()
        proc.terminate()
        gone, alive = psutil.wait_procs([*children, proc], timeout=max(0.1, grace_sec))
        for item in alive:
            item.kill()
        if alive:
            psutil.wait_procs(alive, timeout=1)
        result["terminated"] = not psutil.pid_exists(int(pid))
        result["children"] = len(children)
        return result
    except Exception as exc:
        result["method"] = "taskkill" if os.name == "nt" else "os.kill"
        result["psutil_error"] = str(exc)
    if os.name == "nt":
        proc = subprocess.run(
            ["taskkill.exe", "/PID", str(int(pid)), "/T", "/F"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=WINDOWS_NO_WINDOW,
        )
        result["returncode"] = proc.returncode
        result["terminated"] = not pid_alive(pid)
        return result
    try:
        os.kill(int(pid), 15)
        time.sleep(max(0.1, grace_sec))
        result["terminated"] = not pid_alive(pid)
    except OSError as exc:
        result["error"] = str(exc)
    return result


def process_rss_mb(pid: int) -> float | None:
    try:
        import psutil  # type: ignore

        return float(psutil.Process(int(pid)).memory_info().rss) / 1024.0 / 1024.0
    except Exception:
        pass
    if os.name == "nt":
        script = f"(Get-Process -Id {int(pid)} -ErrorAction SilentlyContinue).WorkingSet64"
        try:
            proc = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", script],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=WINDOWS_NO_WINDOW,
            )
            text = proc.stdout.strip()
            return float(text) / 1024.0 / 1024.0 if text else None
        except Exception:
            return None
    try:
        proc = subprocess.run(["ps", "-o", "rss=", "-p", str(int(pid))], capture_output=True, text=True, timeout=5)
        text = proc.stdout.strip()
        return float(text) / 1024.0 if text else None
    except Exception:
        return None


def read_pid(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except Exception:
        return 0


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        import psutil  # type: ignore

        return psutil.pid_exists(int(pid))
    except Exception:
        pass
    if os.name == "nt":
        script = f"if (Get-Process -Id {int(pid)} -ErrorAction SilentlyContinue) {{ exit 0 }} else {{ exit 1 }}"
        try:
            proc = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", script],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=WINDOWS_NO_WINDOW,
            )
            return proc.returncode == 0
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def cleanup_old_logs(keep_days: float, actions: list[dict[str, Any]], errors: list[str]) -> None:
    if keep_days <= 0:
        return
    cutoff = time.time() - keep_days * 86400.0
    removed = 0
    for path in (ENGINE_DIR / "logs").glob("launcher_*.log"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except Exception as exc:
            errors.append(f"log_cleanup:{path.name}: {exc}")
    if removed:
        actions.append({"ts": utc_now(), "action": "log_cleanup", "removed": removed})


def selected_environments(raw: str) -> list[str]:
    out = []
    for item in str(raw or "").split(","):
        env = item.strip()
        if env in {"personal", "competition"}:
            out.append(env)
    return out or ["personal", "competition"]


def summarize_result(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    keys = ("ok", "already_running", "service", "environment", "started", "errors", "pid", "log_path", "running_count", "planned_count")
    out = {key: value.get(key) for key in keys if key in value}
    if "processes" in value and isinstance(value.get("processes"), list):
        out["process_count"] = len(value.get("processes") or [])
    if "started" in value and isinstance(value.get("started"), list):
        out["started_count"] = len(value.get("started") or [])
    return out


def compact_event(status: dict[str, Any]) -> dict[str, Any]:
    return {
        "ts": status.get("updated_at"),
        "ok": status.get("ok"),
        "cycles": status.get("cycles"),
        "actions": len(status.get("actions") or []),
        "errors": status.get("errors") or [],
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def safe_int(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
