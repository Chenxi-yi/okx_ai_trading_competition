#!/usr/bin/env python3
"""
Print current trading status for both the legacy engine daemon and the current
strategy-runner layout.
Usage: python3 trading_status.py [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
ENGINE_ROOT = PROJECT_ROOT / "engine"
sys.path.insert(0, str(ENGINE_ROOT))
SUMMARY_FILE = PROJECT_ROOT / "engine" / "logs" / "summary.json"
MICRO_LIVE_DIR = PROJECT_ROOT / "engine" / "logs" / "c_auto_v2_micro_live"
RESEARCH_SLEEVES_DIR = PROJECT_ROOT / "engine" / "logs" / "research_sleeves"

try:
    from runtime import EnvironmentRunner
except Exception:
    EnvironmentRunner = None  # type: ignore[assignment]


def read_json(path: Path):
    try:
        return json.loads(path.read_text()) if path.exists() else None
    except Exception:
        return None


def collect_runner_status() -> dict:
    schedulers = []
    for path in sorted(MICRO_LIVE_DIR.glob("*_scheduler.json")) + sorted(RESEARCH_SLEEVES_DIR.glob("*_scheduler.json")):
        payload = read_json(path)
        if not isinstance(payload, dict):
            continue
        state_path = path.with_name(path.name.removesuffix("_scheduler.json") + ".json")
        state = read_json(state_path)
        updated_at = payload.get("updated_at")
        age_sec = age_seconds(updated_at)
        schedulers.append(
            {
                "scheduler_path": str(path.relative_to(PROJECT_ROOT)),
                "state_path": str(state_path.relative_to(PROJECT_ROOT)) if state_path.exists() else None,
                "strategy_id": payload.get("strategy_id"),
                "state_id": payload.get("state_id"),
                "environment": payload.get("environment"),
                "status": payload.get("status"),
                "cycles": payload.get("cycles"),
                "updated_at": updated_at,
                "age_sec": age_sec,
                "fresh": age_sec is not None and age_sec <= 15 * 60,
                "last_error": payload.get("last_error"),
                "execution": payload.get("execution"),
                "okx_profile": payload.get("okx_profile"),
                "nav": state.get("nav") if isinstance(state, dict) else None,
                "positions": len(state.get("positions") or {}) if isinstance(state, dict) else None,
                "candidate_count": state.get("candidate_count") if isinstance(state, dict) else None,
            }
        )

    runner_plans = collect_environment_plans()
    interval_by_key = {
        (item.get("environment"), item.get("strategy_id")): command_interval_sec(" ".join(str(x) for x in item.get("command") or ()))
        for item in runner_plans
    }
    for item in schedulers:
        key = (item.get("environment"), item.get("strategy_id"))
        interval_sec = interval_by_key.get(key)
        if interval_sec is None:
            continue
        item["interval_sec"] = interval_sec
        age_sec = item.get("age_sec")
        if age_sec is not None:
            item["fresh"] = float(age_sec) <= max(15 * 60, interval_sec * 1.25 + 5 * 60)
    processes = collect_processes_from_plans(runner_plans)
    process_error = None
    try:
        scanned = collect_os_processes()
    except Exception as exc:
        process_error = f"{type(exc).__name__}: {exc}"
        scanned = []
    for row in scanned:
        if not any(item.get("pid") == row["pid"] for item in processes):
            processes.append(row)

    planned_by_key = {(item.get("environment"), item.get("strategy_id")): item for item in runner_plans}
    running_by_key = {(item.get("environment"), item.get("strategy_id")) for item in processes}
    for item in schedulers:
        key = (item.get("environment"), item.get("strategy_id"))
        item["process_running"] = key in running_by_key
        if not item["process_running"] and str(item.get("status") or "").lower() == "running":
            item["stale_without_process"] = True
            item["fresh"] = False
    missing_plans = [
        item for key, item in planned_by_key.items()
        if key not in running_by_key
    ]
    readiness_errors = [
        error
        for item in runner_plans
        for error in item.get("readiness_errors", ())
    ]

    lock_or_ps_processes = bool(processes)
    return {
        "mode": "strategy_runners",
        "ok": (not missing_plans and not readiness_errors)
        or any(item.get("status") == "running" and item.get("fresh") for item in schedulers),
        "process_error": None if lock_or_ps_processes else process_error,
        "process_scan_warning": process_error if lock_or_ps_processes else None,
        "processes": processes,
        "runner_plans": runner_plans,
        "missing_plans": missing_plans,
        "readiness_errors": readiness_errors,
        "schedulers": schedulers,
    }


def collect_environment_plans() -> list[dict]:
    if EnvironmentRunner is None:
        return []
    rows = []
    runner = EnvironmentRunner(root=PROJECT_ROOT)
    for environment in ("personal", "competition"):
        try:
            plans = runner.plan(environment)
        except Exception as exc:
            rows.append(
                {
                    "environment": environment,
                    "strategy_id": None,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        for plan in plans:
            existing = list(runner.existing_processes(plan))
            rows.append(
                {
                    "environment": environment,
                    "strategy_id": plan.strategy_id,
                    "state_id": plan.state_id,
                    "runner": plan.runner,
                    "okx_profile": plan.okx_profile,
                    "priority": plan.priority,
                    "readiness_ok": plan.readiness.ok,
                    "readiness_errors": list(plan.readiness.errors),
                    "readiness_checked": list(plan.readiness.checked),
                    "command": list(plan.command),
                    "running": bool(existing),
                    "processes": list(existing),
                }
            )
    return rows


def collect_processes_from_plans(plans: list[dict]) -> list[dict]:
    rows = []
    for plan in plans:
        for process in plan.get("processes") or []:
            pid = process.get("pid")
            if pid is None:
                continue
            try:
                pid_int = int(pid)
            except Exception:
                continue
            if not pid_alive(pid_int):
                continue
            rows.append(
                {
                    "pid": pid_int,
                    "stat": process.get("stat") or "?",
                    "etime": process.get("etime") or "?",
                    "strategy_id": process.get("strategy_id") or plan.get("strategy_id"),
                    "state_id": process.get("state_id") or plan.get("state_id"),
                    "environment": process.get("environment") or plan.get("environment"),
                    "okx_profile": process.get("okx_profile") or plan.get("okx_profile"),
                    "command": process.get("command") or " ".join(str(x) for x in plan.get("command") or ()),
                    "source": process.get("source") or "environment_runner",
                }
            )
    return rows


def collect_os_processes() -> list[dict]:
    if os.name == "nt":
        return collect_windows_processes()
    return collect_posix_processes()


def collect_posix_processes() -> list[dict]:
    proc = subprocess.run(["ps", "-axo", "pid,stat,etime,command"], capture_output=True, text=True, timeout=5)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or f"ps exited {proc.returncode}").strip())
    rows = []
    for line in proc.stdout.splitlines():
        if line.lstrip().startswith("PID "):
            continue
        if "scripts/run_c_auto_v2_micro_live.py" not in line and "scripts/run_research_sleeve_paper.py" not in line:
            continue
        parts = line.strip().split(None, 3)
        if len(parts) < 4:
            continue
        rows.append(process_row(int(parts[0]), parts[3], stat=parts[1], etime=parts[2], source="ps"))
    return rows


def collect_windows_processes() -> list[dict]:
    script = (
        "$rows = Get-CimInstance Win32_Process | "
        "Where-Object { $_.CommandLine -match 'scripts[/\\\\](run_c_auto_v2_micro_live|run_research_sleeve_paper)\\.py' }; "
        "$rows | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
    )
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        timeout=8,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or f"powershell exited {proc.returncode}").strip())
    text = proc.stdout.strip()
    if not text:
        return []
    payload = json.loads(text)
    if isinstance(payload, dict):
        payload = [payload]
    rows = []
    for item in payload if isinstance(payload, list) else []:
        command = str(item.get("CommandLine") or "")
        if not command:
            continue
        rows.append(process_row(int(item.get("ProcessId")), command, stat="win", etime="?", source="windows_process"))
    return rows


def process_row(pid: int, command: str, *, stat: str, etime: str, source: str) -> dict:
    return {
        "pid": pid,
        "stat": stat,
        "etime": etime,
        "strategy_id": command_arg(command, "--strategy-id") or "c_auto_v2_cross_section",
        "state_id": command_arg(command, "--state-id"),
        "environment": command_arg(command, "--environment"),
        "okx_profile": command_arg(command, "--okx-profile"),
        "command": command,
        "source": source,
    }


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError as exc:
        if getattr(exc, "errno", None) == 1:
            return True
        return False


def age_seconds(value) -> float | None:
    if not value:
        return None
    try:
        updated = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - updated.astimezone(timezone.utc)).total_seconds())


def command_arg(command: str, key: str) -> str | None:
    parts = command.split()
    for idx, part in enumerate(parts):
        if part == key and idx + 1 < len(parts):
            return parts[idx + 1]
        if part.startswith(f"{key}="):
            return part.split("=", 1)[1]
    return None


def command_interval_sec(command: str) -> float | None:
    raw = command_arg(command, "--interval-sec")
    if raw is None:
        return None
    try:
        return float(raw)
    except Exception:
        return None


def print_runner_status(status: dict) -> None:
    processes = status.get("processes") or []
    runner_plans = status.get("runner_plans") or []
    schedulers = status.get("schedulers") or []
    print(f"\n{'='*72}")
    print("  STRATEGY RUNNER STATUS")
    print(f"  Plans: {len(runner_plans)}  Processes: {len(processes)}  Schedulers: {len(schedulers)}")
    print(f"{'='*72}")

    if runner_plans:
        print("\n  Registry runtime plan")
        for plan in sorted(runner_plans, key=lambda item: (str(item.get("environment")), -int(item.get("priority") or 0), str(item.get("strategy_id")))):
            readiness = "ready" if plan.get("readiness_ok") else "blocked"
            running = "running" if plan.get("running") else "missing"
            print(
                "    env={env:<11} {running:<7} {ready:<7} profile={profile:<8} priority={priority:<3} {strategy}".format(
                    env=plan.get("environment") or "?",
                    running=running,
                    ready=readiness,
                    profile=plan.get("okx_profile") or "?",
                    priority=plan.get("priority") if plan.get("priority") is not None else "?",
                    strategy=plan.get("strategy_id") or plan.get("error") or "?",
                )
            )
            for err in plan.get("readiness_errors") or []:
                print(f"      readiness_error: {err}")

    if processes:
        print("\n  Running processes")
        for proc in sorted(processes, key=lambda item: (str(item.get("environment")), str(item.get("strategy_id")))):
            print(
                "    PID={pid:<6} {stat:<4} {etime:<9} env={env:<11} profile={profile:<8} source={source:<24} {strategy}".format(
                    pid=proc.get("pid"),
                    stat=proc.get("stat") or "?",
                    etime=proc.get("etime") or "?",
                    env=proc.get("environment") or "?",
                    profile=proc.get("okx_profile") or "?",
                    source=proc.get("source") or "?",
                    strategy=proc.get("strategy_id") or proc.get("state_id") or "?",
                )
            )
    else:
        if status.get("process_error"):
            print(f"\n  Process scan unavailable: {status.get('process_error')}")
        else:
            print("\n  No active runner processes found.")
    if processes and status.get("process_scan_warning"):
        print(f"\n  Process scan warning: {status.get('process_scan_warning')} (lock-based runner status is active)")

    active = [item for item in schedulers if item.get("status") == "running" and item.get("process_running")]
    stale_without_process = [item for item in schedulers if item.get("stale_without_process")]
    if active:
        print("\n  Active schedulers")
        for item in sorted(active, key=lambda row: (str(row.get("environment")), str(row.get("strategy_id")))):
            err = item.get("last_error") or "none"
            freshness = "fresh" if item.get("fresh") else "stale"
            age_min = item.get("age_sec") / 60.0 if item.get("age_sec") is not None else None
            print(
                "    env={env:<11} {fresh:<5} age={age:<7} cycles={cycles:<5} nav={nav!s:<10} pos={pos!s:<3} cand={cand!s:<4} {strategy}".format(
                    env=item.get("environment") or "?",
                    fresh=freshness,
                    age=(f"{age_min:.1f}m" if age_min is not None else "?"),
                    cycles=item.get("cycles") if item.get("cycles") is not None else "?",
                    nav=item.get("nav") if item.get("nav") is not None else "?",
                    pos=item.get("positions") if item.get("positions") is not None else "?",
                    cand=item.get("candidate_count") if item.get("candidate_count") is not None else "?",
                    strategy=item.get("strategy_id") or item.get("state_id") or "?",
                )
            )
            if err != "none":
                print(f"      last_error: {err}")
    if stale_without_process:
        print("\n  Stale scheduler files without live process")
        for item in sorted(stale_without_process, key=lambda row: (str(row.get("environment")), str(row.get("strategy_id"))))[:12]:
            age_min = item.get("age_sec") / 60.0 if item.get("age_sec") is not None else None
            print(
                "    env={env:<11} age={age:<7} cycles={cycles:<5} {strategy}".format(
                    env=item.get("environment") or "?",
                    age=(f"{age_min:.1f}m" if age_min is not None else "?"),
                    cycles=item.get("cycles") if item.get("cycles") is not None else "?",
                    strategy=item.get("strategy_id") or item.get("state_id") or "?",
                )
            )
    print(f"\n{'='*72}\n")


def main():
    p = argparse.ArgumentParser(description="Show current trading status")
    p.add_argument("--json", action="store_true", help="Output raw JSON")
    args = p.parse_args()

    runner_status = collect_runner_status()
    summary = read_json(SUMMARY_FILE)
    runner_has_truth = bool(runner_status.get("runner_plans") or runner_status.get("processes") or runner_status.get("schedulers"))

    if args.json:
        print(
            json.dumps(
                {
                    "mode": "strategy_runners",
                    "runner": runner_status,
                    "legacy_summary": summary,
                    "legacy_summary_path": str(SUMMARY_FILE.relative_to(PROJECT_ROOT)),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return

    if runner_has_truth:
        print_runner_status(runner_status)
        if summary:
            updated_at = summary.get("updated_at")
            age_sec = age_seconds(updated_at)
            freshness = f"{age_sec / 60.0:.1f}m old" if age_sec is not None else "unknown age"
            print(
                "  Legacy engine summary is compatibility-only: "
                f"status={str(summary.get('engine_status', 'unknown')).upper()} updated={updated_at or '?'} ({freshness})\n"
            )
        if not runner_status.get("ok"):
            sys.exit(1)
        return

    if not isinstance(summary, dict):
        print(f"No strategy runner status found and could not parse {SUMMARY_FILE}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
        return

    updated = summary.get("updated_at", "?")
    status  = summary.get("engine_status", "unknown").upper()
    pid     = summary.get("pid", "?")

    print(f"\n{'='*58}")
    print(f"  ENGINE STATUS: {status}  PID={pid}")
    print(f"  Updated: {updated}")
    print(f"{'='*58}")

    portfolios = summary.get("portfolios", {})
    if not portfolios:
        print("  No active portfolios.")
    else:
        for pid_name, snap in portfolios.items():
            nav     = snap.get("nav", 0)
            capital = snap.get("capital", 0)
            pnl     = snap.get("pnl", 0)
            pnl_pct = snap.get("pnl_pct", 0)
            dd      = snap.get("drawdown_pct", 0)
            n_pos   = snap.get("n_positions", 0)
            risk    = snap.get("risk", {})
            sign    = "+" if pnl >= 0 else ""
            print(f"\n  [{pid_name}]")
            print(f"    NAV:      ${nav:>9,.2f}  (capital ${capital:,.2f})")
            print(f"    PnL:      {sign}${pnl:>8,.2f}  ({sign}{pnl_pct:.2f}%)")
            print(f"    Drawdown: {dd:+.1f}%  |  Positions: {n_pos}")
            print(f"    Risk:     CB={risk.get('cb','?')}  Vol={risk.get('vol','?')}")
            last_reb = snap.get("last_rebalance", "Never")
            print(f"    Last reb: {last_reb}")

    total_nav = summary.get("total_nav", 0)
    total_pnl = summary.get("total_pnl", 0)
    total_pct = summary.get("total_pnl_pct", 0)
    sign = "+" if total_pnl >= 0 else ""
    print(f"\n{'─'*58}")
    print(f"  TOTAL NAV: ${total_nav:,.2f}  PnL: {sign}${total_pnl:,.2f} ({sign}{total_pct:.2f}%)")
    print(f"{'='*58}\n")

    if status != "RUNNING":
        sys.exit(1)


if __name__ == "__main__":
    main()
