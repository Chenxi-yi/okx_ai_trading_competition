"""Registry-driven environment runner.

This module is the canonical runtime entry point for personal/competition
startup. It owns process orchestration only; strategy logic, committee
decisions, position management, risk, execution, and accounting live elsewhere.
"""

from __future__ import annotations

import os
import json
import errno
import subprocess
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

try:
    from config.settings import BASE_DIR
    from data.frame_store import FRAME_SUFFIXES, frame_health
    from registry import StrategyRecord, StrategyRegistry
except ModuleNotFoundError:
    from engine.config.settings import BASE_DIR
    from engine.data.frame_store import FRAME_SUFFIXES, frame_health
    from engine.registry import StrategyRecord, StrategyRegistry


ALLOWED_ENVIRONMENTS = {"personal", "competition"}
ENVIRONMENT_OKX_PROFILE = {
    "competition": "live",
    "personal": "personal",
}
OKX_ENV_CREDENTIAL_KEYS = {
    "OKX_API_KEY",
    "OKX_SECRET_KEY",
    "OKX_API_SECRET",
    "OKX_PASSPHRASE",
}
WINDOWS_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


@dataclass(frozen=True)
class ReadinessResult:
    ok: bool
    strategy_id: str
    checked: tuple[dict[str, object], ...] = ()
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class StrategyLaunchPlan:
    strategy_id: str
    environment: str
    runner: str
    state_id: str
    command: tuple[str, ...]
    okx_profile: str
    priority: int
    readiness: ReadinessResult
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class StrategyLaunchResult:
    plan: StrategyLaunchPlan
    started: bool
    already_running: bool = False
    pid: int | None = None
    error: str | None = None
    log_path: str | None = None


@dataclass(frozen=True)
class EnvironmentRunResult:
    environment: str
    plans: tuple[StrategyLaunchPlan, ...]
    results: tuple[StrategyLaunchResult, ...]

    @property
    def ok(self) -> bool:
        return all(item.error is None for item in self.results)


@dataclass(frozen=True)
class StrategyStopResult:
    plan: StrategyLaunchPlan
    stopped_pids: tuple[int, ...] = ()
    stop_files: tuple[str, ...] = ()
    terminations: tuple[dict[str, object], ...] = ()
    remaining: tuple[Mapping[str, object], ...] = ()

    @property
    def ok(self) -> bool:
        return not self.remaining


ProcessStarter = Callable[[Sequence[str], Mapping[str, str]], subprocess.Popen]
ProcessFinder = Callable[[StrategyLaunchPlan], Sequence[Mapping[str, object]]]


class DataReadinessProbe:
    """Checks declared Strategy Office data dependencies.

    The first implementation is intentionally conservative and file-based.
    Dataset-specific probes can be added here without leaking readiness checks
    into strategies.
    """

    def __init__(self, root: Path | str = BASE_DIR.parent):
        self.root = Path(root)

    def check(self, record: StrategyRecord) -> ReadinessResult:
        errors: list[str] = []
        checked: list[dict[str, object]] = []
        now = time.time()
        for dep in record.data_dependencies:
            if not dep.required:
                continue
            path = self._dependency_path(dep.path)
            row: dict[str, object] = {
                "dependency_id": dep.dependency_id,
                "kind": dep.kind,
                "path": str(path) if path else "",
                "dataset_id": dep.dataset_id,
                "required": dep.required,
            }
            if path:
                if not path.exists():
                    errors.append(f"{dep.dependency_id}: missing {path}")
                    checked.append(row | {"exists": False})
                    continue
                age_sec = max(0.0, now - path.stat().st_mtime)
                row.update({"exists": True, "age_sec": age_sec})
                if dep.max_age_sec is not None and age_sec > dep.max_age_sec:
                    errors.append(f"{dep.dependency_id}: stale age_sec={age_sec:.0f} max={dep.max_age_sec:.0f}")
                if dep.kind == "scheduler_status":
                    self._apply_scheduler_status_checks(dep, row, path, errors)
                self._apply_frame_checks(dep, row, path, errors)
                manifest = self._manifest_for_path(path)
                self._apply_manifest_checks(dep, row, manifest, errors)
            elif dep.dataset_id:
                manifest = self._manifest_for_dataset(dep.dataset_id)
                if manifest:
                    row["exists"] = True
                    self._apply_manifest_checks(dep, row, manifest, errors)
                else:
                    errors.append(f"{dep.dependency_id}: dataset manifest not found for {dep.dataset_id}")
                    row["exists"] = False
            else:
                errors.append(f"{dep.dependency_id}: missing path or dataset_id")
            checked.append(row)
        return ReadinessResult(
            ok=not errors,
            strategy_id=record.strategy_id,
            checked=tuple(checked),
            errors=tuple(errors),
        )

    def _apply_frame_checks(
        self,
        dep: object,
        row: dict[str, object],
        path: Path,
        errors: list[str],
    ) -> None:
        if path.is_dir() or path.suffix.lower() not in FRAME_SUFFIXES:
            return
        health = frame_health(path)
        row["readable"] = health.ok
        row["frame_reader"] = health.reader
        row["frame_rows"] = health.rows
        if health.columns:
            row["frame_columns_sample"] = list(health.columns[:12])
        if not health.ok:
            row["frame_error"] = health.error
            errors.append(f"{dep.dependency_id}: unreadable {path.name}: {health.error}")

    def _apply_scheduler_status_checks(
        self,
        dep: object,
        row: dict[str, object],
        path: Path,
        errors: list[str],
    ) -> None:
        status = self._read_json_file(path)
        if not status:
            row["scheduler_readable"] = False
            errors.append(f"{dep.dependency_id}: scheduler status unreadable")
            return
        row["scheduler_readable"] = True
        scheduler_status = str(status.get("scheduler_status") or status.get("status") or "").lower()
        ok_count = self._int_value(status.get("ok"))
        failed_count = self._int_value(status.get("failed"))
        total_jobs = self._int_value(status.get("total_jobs"))
        row["scheduler_status"] = scheduler_status
        row["scheduler_ok_jobs"] = ok_count
        row["scheduler_failed_jobs"] = failed_count
        row["scheduler_total_jobs"] = total_jobs
        last_record = status.get("last_record")
        if isinstance(last_record, Mapping):
            row["last_refresh_record"] = {
                key: last_record.get(key)
                for key in ("status", "kind", "symbol", "timeframe", "fresh", "freshness_error", "error", "cache_after")
                if key in last_record
            }
        if scheduler_status != "running":
            errors.append(f"{dep.dependency_id}: scheduler_status={scheduler_status or 'missing'}")
        if total_jobs > 0 and ok_count <= 0:
            errors.append(f"{dep.dependency_id}: no successful refresh jobs")
        if ok_count <= 0 and failed_count > 0:
            errors.append(f"{dep.dependency_id}: refresh failing failed={failed_count}")

    def _dependency_path(self, value: str) -> Path | None:
        if not value:
            return None
        path = Path(value)
        return path if path.is_absolute() else self.root / path

    def _manifest_for_path(self, path: Path) -> Mapping[str, object]:
        candidates = []
        if path.name == "manifest.json":
            candidates.append(path)
        if path.is_dir():
            candidates.append(path / "manifest.json")
        else:
            candidates.append(path.with_name("manifest.json"))
        for candidate in candidates:
            try:
                data = json.loads(candidate.read_text())
            except Exception:
                continue
            if isinstance(data, Mapping):
                return data
        return {}

    def _read_json_file(self, path: Path) -> Mapping[str, object]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, Mapping) else {}

    def _manifest_for_dataset(self, dataset_id: str) -> Mapping[str, object]:
        catalog_path = self.root / "engine" / "data" / "catalog.json"
        try:
            catalog = json.loads(catalog_path.read_text())
        except Exception:
            catalog = {}
        for item in catalog.get("datasets", []) if isinstance(catalog, Mapping) else []:
            if isinstance(item, Mapping) and str(item.get("dataset_id") or "") == dataset_id:
                manifest = item.get("manifest")
                return manifest if isinstance(manifest, Mapping) else item
        for base in (self.root / "engine" / "data").glob("**/manifest.json"):
            try:
                data = json.loads(base.read_text())
            except Exception:
                continue
            if isinstance(data, Mapping) and str(data.get("dataset_id") or "") == dataset_id:
                return data
        return {}

    def _apply_manifest_checks(
        self,
        dep: object,
        row: dict[str, object],
        manifest: Mapping[str, object],
        errors: list[str],
    ) -> None:
        if not manifest:
            return
        row["manifest"] = {
            key: manifest.get(key)
            for key in ("dataset_id", "created_at", "completed_at", "rows", "symbols")
            if key in manifest
        }
        shape = manifest.get("shape")
        if isinstance(shape, Mapping):
            row["manifest_shape"] = dict(shape)
        quality = manifest.get("quality") or manifest.get("summary")
        if isinstance(quality, Mapping):
            row["manifest_quality"] = {
                key: quality.get(key)
                for key in ("coverage_min", "coverage_median", "low_coverage_jobs", "rows", "symbols")
                if key in quality
            }
        min_rows = dep.metadata.get("min_rows")
        rows = self._manifest_rows(manifest)
        if rows is not None:
            row["rows"] = rows
        if min_rows is not None and rows is not None and rows < float(min_rows):
            errors.append(f"{dep.dependency_id}: rows={rows:g} min_rows={float(min_rows):g}")
        min_coverage = dep.metadata.get("min_coverage")
        coverage = self._manifest_coverage(manifest)
        if coverage is not None:
            row["coverage"] = coverage
        if min_coverage is not None and coverage is not None and coverage < float(min_coverage):
            errors.append(f"{dep.dependency_id}: coverage={coverage:g} min_coverage={float(min_coverage):g}")

    @staticmethod
    def _manifest_rows(manifest: Mapping[str, object]) -> float | None:
        for key in ("rows", "row_count"):
            value = manifest.get(key)
            if isinstance(value, (int, float)):
                return float(value)
        shape = manifest.get("shape")
        if isinstance(shape, Mapping):
            for key in ("rows", "feature_rows", "label_rows"):
                value = shape.get(key)
                if isinstance(value, (int, float)):
                    return float(value)
        summary = manifest.get("summary")
        if isinstance(summary, Mapping):
            value = summary.get("rows")
            if isinstance(value, (int, float)):
                return float(value)
        return None

    @staticmethod
    def _manifest_coverage(manifest: Mapping[str, object]) -> float | None:
        for container in (manifest, manifest.get("summary")):
            if not isinstance(container, Mapping):
                continue
            for key in ("coverage_min", "coverage", "coverage_median"):
                value = container.get(key)
                if isinstance(value, (int, float)):
                    return float(value)
        return None

    @staticmethod
    def _int_value(value: object) -> int:
        try:
            return int(float(value or 0))
        except Exception:
            return 0


class EnvironmentRunner:
    """Starts registry-selected strategy adapters for one environment."""

    def __init__(
        self,
        registry: StrategyRegistry | None = None,
        readiness: DataReadinessProbe | None = None,
        process_finder: ProcessFinder | None = None,
        process_starter: ProcessStarter | None = None,
        root: Path | str = BASE_DIR.parent,
    ):
        self.registry = registry or StrategyRegistry()
        self.readiness = readiness or DataReadinessProbe(root)
        self.process_finder = process_finder or self._default_process_finder
        self.process_starter = process_starter or self._default_process_starter
        self.root = Path(root)

    def plan(self, environment: str) -> tuple[StrategyLaunchPlan, ...]:
        self._validate_environment(environment)
        plans: list[StrategyLaunchPlan] = []
        for record in self.registry.runnable_strategies(environment):
            readiness = self.readiness.check(record)
            plans.append(self._plan_record(record, environment, readiness))
        return tuple(plans)

    def existing_processes(self, plan: StrategyLaunchPlan) -> tuple[Mapping[str, object], ...]:
        return tuple(self.process_finder(plan))

    def start(self, environment: str, *, confirm_real: bool = False) -> EnvironmentRunResult:
        self._validate_environment(environment)
        if not confirm_real:
            raise ValueError("environment runner requires confirm_real=true")
        plans = self.plan(environment)
        results = tuple(self._start_plan(plan) for plan in plans)
        self.write_status(environment, plans=plans, results=results)
        return EnvironmentRunResult(environment=environment, plans=plans, results=results)

    def stop(self, environment: str) -> dict[str, object]:
        """Stop all registry-managed runner processes for one environment."""
        self._validate_environment(environment)
        plans = self.plan(environment)
        results = tuple(self._stop_plan(plan) for plan in plans)
        remaining = [dict(proc) for plan in plans for proc in self.existing_processes(plan)]
        self.write_status(environment, plans=plans)
        return {
            "ok": not remaining,
            "environment": environment,
            "mode": "environment_runner_stop",
            "stopped_pids": [pid for result in results for pid in result.stopped_pids],
            "stop_files": [path for result in results for path in result.stop_files],
            "terminations": [item for result in results for item in result.terminations],
            "remaining_processes": remaining,
            "strategies": [self._stop_result_row(result) for result in results],
        }

    def status(self, environment: str, *, plans: Sequence[StrategyLaunchPlan] | None = None) -> dict[str, object]:
        """Return structured runner truth for control-plane consumers."""
        self._validate_environment(environment)
        selected_plans = tuple(plans) if plans is not None else self.plan(environment)
        rows = [self._status_row(plan) for plan in selected_plans]
        blocked = [row for row in rows if not row.get("readiness", {}).get("ok")]
        running = [row for row in rows if row.get("running") and row.get("scheduler", {}).get("fresh", True)]
        return {
            "ok": not blocked and len(running) == len(rows),
            "environment": environment,
            "updated_at": self._utc_now_iso(),
            "planned_count": len(rows),
            "running_count": len(running),
            "blocked_count": len(blocked),
            "strategies": rows,
        }

    def write_status(
        self,
        environment: str,
        *,
        plans: Sequence[StrategyLaunchPlan] | None = None,
        results: Sequence[StrategyLaunchResult] | None = None,
    ) -> dict[str, object]:
        payload = self.status(environment, plans=plans)
        if results is not None:
            payload["launch_results"] = [self._launch_result_row(item) for item in results]
            payload["ok"] = bool(payload.get("ok")) and all(item.error is None for item in results)
        path = self._status_dir() / f"{environment}_status.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return payload

    def _plan_record(
        self,
        record: StrategyRecord,
        environment: str,
        readiness: ReadinessResult,
    ) -> StrategyLaunchPlan:
        runtime = record.runtime
        runner = runtime.runner
        state_id = runtime.state_id or record.strategy_id
        profile = ENVIRONMENT_OKX_PROFILE[environment]
        parameter_set_id = record.default_parameter_set_id or ""
        if runner == "c_auto_v2_micro_live":
            command = (
                self._python_bin(),
                "scripts/run_c_auto_v2_micro_live.py",
                "--state-id",
                state_id,
                "--paper-state-id",
                "fixed1000_conservative",
                "--environment",
                environment,
                "--okx-profile",
                profile,
                "--initial-capital",
                "3000",
                "--fixed-notional-capital",
                "3000",
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
                "--confirm-micro-live",
                "--interval-sec",
                str(runtime.interval_sec),
                "--entry-scan-minutes",
                "15",
                "--run-on-start-entry",
            )
        elif runner in {"research_sleeve_live", "scripts.run_research_sleeve_paper", "scripts.run_trend_pullback_reversal_variants"}:
            capital = self._runtime_capital(parameter_set_id)
            command = (
                self._python_bin(),
                "scripts/run_research_sleeve_paper.py",
                "--strategy-id",
                record.strategy_id,
                "--state-id",
                state_id,
                "--environment",
                environment,
                "--initial-capital",
                f"{capital:g}",
                "--execution",
                "live",
                "--okx-profile",
                profile,
                "--parameter-set-id",
                parameter_set_id,
                "--loop",
                "--interval-sec",
                str(runtime.interval_sec),
            )
        else:
            raise ValueError(f"unsupported runtime runner for {record.strategy_id}: {runner or 'missing'}")
        return StrategyLaunchPlan(
            strategy_id=record.strategy_id,
            environment=environment,
            runner=runner,
            state_id=state_id,
            command=command,
            okx_profile=profile,
            priority=runtime.priority,
            readiness=readiness,
            metadata={"parameter_set_id": parameter_set_id, **dict(runtime.metadata)},
        )

    def _start_plan(self, plan: StrategyLaunchPlan) -> StrategyLaunchResult:
        if not plan.readiness.ok:
            return StrategyLaunchResult(plan=plan, started=False, error="; ".join(plan.readiness.errors))
        existing = list(self.existing_processes(plan))
        if existing:
            pid = existing[0].get("pid")
            return StrategyLaunchResult(
                plan=plan,
                started=False,
                already_running=True,
                pid=int(pid) if pid is not None else None,
            )
        try:
            self._clear_start_blockers(plan)
            log_path = self._launch_log_path_for_plan(plan)
            env = self._okx_cli_env(plan.okx_profile)
            env["OKX_RUNNER_LOG_PATH"] = str(log_path)
            proc = self.process_starter(plan.command, env)
            return StrategyLaunchResult(
                plan=plan,
                started=True,
                pid=int(proc.pid),
                log_path=str(log_path.relative_to(self.root)),
            )
        except Exception as exc:
            return StrategyLaunchResult(plan=plan, started=False, error=str(exc))

    def _stop_plan(self, plan: StrategyLaunchPlan) -> StrategyStopResult:
        processes = self._dedupe_processes(self.existing_processes(plan))
        if not processes:
            return StrategyStopResult(plan=plan)
        stop_path = self._stop_file(plan)
        stop_path.parent.mkdir(parents=True, exist_ok=True)
        stop_path.write_text(time.strftime("%Y-%m-%dT%H:%M:%S%z"), encoding="utf-8")
        stopped: list[int] = []
        terminations: list[dict[str, object]] = []
        for proc in processes:
            termination = self._terminate_process(proc.get("pid"))
            terminations.append(termination)
            if termination.get("terminated"):
                try:
                    stopped.append(int(proc.get("pid") or 0))
                except Exception:
                    pass
        remaining = self._dedupe_processes(self.existing_processes(plan))
        if not remaining:
            self._remove_exclusive_locks(plan)
        return StrategyStopResult(
            plan=plan,
            stopped_pids=tuple(pid for pid in stopped if pid > 0),
            stop_files=(str(stop_path.relative_to(self.root)),),
            terminations=tuple(terminations),
            remaining=remaining,
        )

    def _status_row(self, plan: StrategyLaunchPlan) -> dict[str, object]:
        processes = [dict(item) for item in self.existing_processes(plan)]
        scheduler = self._scheduler_snapshot(plan)
        scheduler = self._scheduler_health(plan, scheduler)
        if not processes and self._scheduler_claims_running(scheduler):
            scheduler = dict(scheduler)
            scheduler["stale_without_process"] = True
            scheduler["display_status"] = "stopped"
        state = self._state_snapshot(plan)
        reconciliation = self._ownership_reconciliation_snapshot(plan.environment)
        return {
            "strategy_id": plan.strategy_id,
            "environment": plan.environment,
            "runner": plan.runner,
            "state_id": plan.state_id,
            "okx_profile": plan.okx_profile,
            "priority": plan.priority,
            "running": bool(processes),
            "processes": processes,
            "readiness": {
                "ok": plan.readiness.ok,
                "checked": list(plan.readiness.checked),
                "errors": list(plan.readiness.errors),
            },
            "scheduler": scheduler,
            "position": {
                "open_positions": self._open_position_count(state),
                "nav": state.get("nav") if isinstance(state, dict) else None,
                "open_risk": state.get("open_risk") if isinstance(state, dict) else None,
            },
            "committee": {
                "candidate_count": state.get("candidate_count") if isinstance(state, dict) else None,
                "last_decision": state.get("last_decision") if isinstance(state, dict) else None,
            },
            "execution": scheduler.get("execution") if isinstance(scheduler, dict) else None,
            "accounting": {
                "ownership_reconciled": reconciliation.get("ok") if reconciliation else None,
                "owned_positions": reconciliation.get("owned_count") if reconciliation else None,
                "exchange_positions": reconciliation.get("exchange_count") if reconciliation else None,
                "errors": reconciliation.get("errors") if reconciliation else [],
                "updated_at": reconciliation.get("updated_at") if reconciliation else None,
            },
        }

    def _launch_result_row(self, result: StrategyLaunchResult) -> dict[str, object]:
        return {
            "strategy_id": result.plan.strategy_id,
            "environment": result.plan.environment,
            "started": result.started,
            "already_running": result.already_running,
            "pid": result.pid,
            "error": result.error,
            "log_path": result.log_path,
        }

    def _stop_result_row(self, result: StrategyStopResult) -> dict[str, object]:
        return {
            "strategy_id": result.plan.strategy_id,
            "environment": result.plan.environment,
            "runner": result.plan.runner,
            "stopped_pids": list(result.stopped_pids),
            "stop_files": list(result.stop_files),
            "terminations": list(result.terminations),
            "remaining": [dict(item) for item in result.remaining],
            "ok": result.ok,
        }

    def _scheduler_snapshot(self, plan: StrategyLaunchPlan) -> dict[str, object]:
        return self._read_json(self._state_file(plan, suffix="_scheduler.json"))

    def _scheduler_health(self, plan: StrategyLaunchPlan, scheduler: Mapping[str, object]) -> dict[str, object]:
        row = dict(scheduler)
        updated_at = row.get("updated_at")
        age_sec = self._age_seconds(updated_at)
        if age_sec is not None:
            row["age_sec"] = age_sec
            interval_sec = self._plan_interval_sec(plan)
            row["interval_sec"] = interval_sec
            row["fresh"] = age_sec <= max(15 * 60, interval_sec * 1.25 + 5 * 60)
        return row

    def _state_snapshot(self, plan: StrategyLaunchPlan) -> dict[str, object]:
        return self._read_json(self._state_file(plan, suffix=".json"))

    def _state_file(self, plan: StrategyLaunchPlan, *, suffix: str) -> Path:
        if plan.runner == "c_auto_v2_micro_live":
            prefix = f"{plan.state_id}_{plan.environment}"
            return self.root / "engine" / "logs" / "c_auto_v2_micro_live" / f"{prefix}{suffix}"
        prefix = f"{plan.state_id}_{plan.environment}"
        return self.root / "engine" / "logs" / "research_sleeves" / f"{prefix}{suffix}"

    def _stop_file(self, plan: StrategyLaunchPlan) -> Path:
        if plan.runner == "c_auto_v2_micro_live":
            return self.root / "engine" / "control" / f"c_auto_v2_micro_live_{plan.state_id}_{plan.environment}.stop"
        return self.root / "engine" / "control" / f"research_sleeve_{plan.state_id}_{plan.environment}.stop"

    def _clear_start_blockers(self, plan: StrategyLaunchPlan) -> None:
        try:
            self._stop_file(plan).unlink()
        except FileNotFoundError:
            pass
        except Exception:
            pass
        self._remove_exclusive_locks(plan)

    def _read_json(self, path: Path) -> dict[str, object]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _ownership_reconciliation_snapshot(self, environment: str) -> dict[str, object]:
        return self._read_json(
            self.root / "engine" / "logs" / "ownership" / str(environment) / "reconciliation_status.json"
        )

    @staticmethod
    def _scheduler_claims_running(scheduler: Mapping[str, object]) -> bool:
        status = str(scheduler.get("status") or scheduler.get("scheduler_status") or "").lower()
        return status == "running"

    def _status_dir(self) -> Path:
        return self.root / "engine" / "logs" / "environment_runner"

    @staticmethod
    def _open_position_count(state: Mapping[str, object]) -> int | None:
        if not isinstance(state, Mapping):
            return None
        positions = state.get("positions")
        if isinstance(positions, Mapping):
            return len(positions)
        if isinstance(positions, Sequence) and not isinstance(positions, (str, bytes)):
            return len(positions)
        return None

    @staticmethod
    def _utc_now_iso() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    @staticmethod
    def _age_seconds(value: object) -> float | None:
        if not value:
            return None
        try:
            ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds())

    @staticmethod
    def _plan_interval_sec(plan: StrategyLaunchPlan) -> float:
        command = " ".join(str(item) for item in plan.command)
        raw = EnvironmentRunner._command_arg(command, "--interval-sec")
        try:
            return float(raw or 0.0)
        except Exception:
            return 0.0

    def _runtime_capital(self, parameter_set_id: str) -> float:
        if not parameter_set_id:
            return 50.0
        try:
            params = self.registry.get_parameter_set(parameter_set_id).params
        except KeyError:
            return 50.0
        return float(params.get("runtime_budget_usdt") or params.get("capital_usdt") or 50.0)

    def _default_process_finder(self, plan: StrategyLaunchPlan) -> Sequence[Mapping[str, object]]:
        lock_match = self._process_from_exclusive_lock(plan)
        if lock_match:
            return (lock_match,)
        if os.name == "nt":
            return self._default_windows_process_finder(plan)
        try:
            proc = subprocess.run(
                ["ps", "-axo", "pid=,command="],
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=5,
            )
        except Exception:
            return ()
        if proc.returncode != 0:
            return ()
        matches: list[dict[str, object]] = []
        for line in proc.stdout.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if "scripts/run_research_sleeve_paper.py" not in stripped and "scripts/run_c_auto_v2_micro_live.py" not in stripped:
                continue
            try:
                pid_raw, command = stripped.split(None, 1)
                pid = int(pid_raw)
            except ValueError:
                continue
            found_strategy = self._command_arg(command, "--strategy-id")
            found_state = self._command_arg(command, "--state-id")
            found_env = self._command_arg(command, "--environment")
            if found_env == plan.environment and (found_strategy == plan.strategy_id or found_state == plan.state_id):
                matches.append({"pid": pid, "command": command, "environment": found_env})
        return tuple(matches)

    def _default_windows_process_finder(self, plan: StrategyLaunchPlan) -> Sequence[Mapping[str, object]]:
        script = (
            "$rows = Get-CimInstance Win32_Process -Filter \"name = 'python.exe'\" | "
            "Where-Object { $_.CommandLine -match 'scripts[/\\\\](run_research_sleeve_paper|run_c_auto_v2_micro_live)\\.py' }; "
            "$rows | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
        )
        try:
            proc = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", script],
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=8,
                creationflags=WINDOWS_NO_WINDOW,
            )
        except Exception:
            return ()
        if proc.returncode != 0 or not proc.stdout.strip():
            return ()
        try:
            payload = json.loads(proc.stdout)
        except Exception:
            return ()
        if isinstance(payload, dict):
            payload = [payload]
        matches: list[dict[str, object]] = []
        for item in payload if isinstance(payload, list) else []:
            command = str(item.get("CommandLine") or "")
            if not command:
                continue
            found_strategy = self._command_arg(command, "--strategy-id")
            found_state = self._command_arg(command, "--state-id")
            found_env = self._command_arg(command, "--environment")
            if found_env == plan.environment and (found_strategy == plan.strategy_id or found_state == plan.state_id):
                matches.append({"pid": int(item.get("ProcessId")), "command": command, "environment": found_env})
        return tuple(matches)

    def _process_from_exclusive_lock(self, plan: StrategyLaunchPlan) -> Mapping[str, object] | None:
        for path in self._exclusive_lock_paths(plan):
            try:
                data = json.loads(path.read_text())
            except Exception:
                continue
            try:
                pid = int(data.get("pid") or 0)
            except Exception:
                continue
            if pid <= 0 or not self._pid_alive(pid):
                continue
            if str(data.get("environment") or "") != plan.environment:
                continue
            return {
                "pid": pid,
                "environment": plan.environment,
                "strategy_id": plan.strategy_id,
                "lock_strategy_id": data.get("strategy_id") or plan.strategy_id,
                "source": "exclusive_strategy_lock",
                "lock_path": str(path),
            }
        return None

    def _exclusive_lock_paths(self, plan: StrategyLaunchPlan) -> tuple[Path, ...]:
        paths: list[Path] = []
        for strategy_id in self._lock_strategy_ids(plan):
            safe_strategy = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in strategy_id)
            paths.append(self.root / "engine" / "control" / f"exclusive_strategy_{safe_strategy}.lock")
        return tuple(dict.fromkeys(paths))

    def _remove_exclusive_locks(self, plan: StrategyLaunchPlan) -> None:
        for path in self._exclusive_lock_paths(plan):
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            except Exception:
                continue

    @staticmethod
    def _dedupe_processes(processes: Sequence[Mapping[str, object]]) -> tuple[Mapping[str, object], ...]:
        seen: set[int] = set()
        out: list[Mapping[str, object]] = []
        for proc in processes:
            try:
                pid = int(proc.get("pid") or 0)
            except Exception:
                pid = 0
            if pid <= 0 or pid in seen:
                continue
            seen.add(pid)
            out.append(proc)
        return tuple(out)

    @staticmethod
    def _lock_strategy_ids(plan: StrategyLaunchPlan) -> tuple[str, ...]:
        raw = plan.metadata.get("lock_strategy_id") or plan.metadata.get("exclusive_strategy_id")
        values = [plan.strategy_id]
        if raw:
            values.append(str(raw))
        return tuple(dict.fromkeys(values))

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if os.name == "nt":
            try:
                proc = subprocess.run(
                    ["powershell.exe", "-NoProfile", "-Command", f"Get-CimInstance Win32_Process -Filter \"ProcessId = {int(pid)}\""],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    creationflags=WINDOWS_NO_WINDOW,
                )
                return proc.returncode == 0 and bool(proc.stdout.strip())
            except Exception:
                return False
        try:
            os.kill(pid, 0)
            return True
        except OSError as exc:
            if getattr(exc, "errno", None) == errno.EPERM:
                return True
            return False

    def _terminate_process(self, pid: object, *, grace_sec: float = 3.0) -> dict[str, object]:
        if not pid:
            return {"pid": pid, "terminated": False, "reason": "missing_pid"}
        try:
            pid_int = int(pid)
        except Exception:
            return {"pid": pid, "terminated": False, "reason": "invalid_pid"}
        if not self._pid_alive(pid_int):
            return {"pid": pid_int, "terminated": True, "already_stopped": True}
        if os.name == "nt":
            return self._terminate_windows_process(pid_int, grace_sec=grace_sec)
        result: dict[str, object] = {"pid": pid_int, "terminated": False, "signal": "TERM", "escalated": False}
        try:
            try:
                os.killpg(pid_int, 15)
                result["target"] = "process_group"
            except OSError:
                os.kill(pid_int, 15)
                result["target"] = "process"
        except OSError as exc:
            result["error"] = str(exc)
            return result
        deadline = time.time() + max(0.1, grace_sec)
        while time.time() < deadline:
            if not self._pid_alive(pid_int):
                result["terminated"] = True
                return result
            time.sleep(0.1)
        result["escalated"] = True
        result["signal"] = "KILL"
        try:
            try:
                os.killpg(pid_int, 9)
            except OSError:
                os.kill(pid_int, 9)
        except OSError as exc:
            result["error"] = str(exc)
        time.sleep(0.2)
        result["terminated"] = not self._pid_alive(pid_int)
        return result

    def _terminate_windows_process(self, pid: int, *, grace_sec: float) -> dict[str, object]:
        result: dict[str, object] = {"pid": pid, "terminated": False, "signal": "TERM", "escalated": False, "target": "process_tree"}
        proc = subprocess.run(
            ["taskkill.exe", "/PID", str(pid), "/T"],
            cwd=str(self.root),
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=WINDOWS_NO_WINDOW,
        )
        result["returncode"] = proc.returncode
        if proc.stdout.strip():
            result["stdout"] = proc.stdout.strip()
        if proc.stderr.strip():
            result["stderr"] = proc.stderr.strip()
        deadline = time.time() + max(0.1, grace_sec)
        while time.time() < deadline:
            if not self._pid_alive(pid):
                result["terminated"] = True
                return result
            time.sleep(0.1)
        result["escalated"] = True
        result["signal"] = "KILL"
        forced = subprocess.run(
            ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
            cwd=str(self.root),
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=WINDOWS_NO_WINDOW,
        )
        result["force_returncode"] = forced.returncode
        if forced.stdout.strip():
            result["force_stdout"] = forced.stdout.strip()
        if forced.stderr.strip():
            result["force_stderr"] = forced.stderr.strip()
        time.sleep(0.2)
        result["terminated"] = not self._pid_alive(pid)
        return result

    def _default_process_starter(self, command: Sequence[str], env: Mapping[str, str]) -> subprocess.Popen:
        popen_kwargs = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        log_path = Path(str(env.get("OKX_RUNNER_LOG_PATH") or "")) if env.get("OKX_RUNNER_LOG_PATH") else self._launch_log_path_from_command(command)
        if not log_path.is_absolute():
            log_path = self.root / log_path
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = log_path.open("a", buffering=1, encoding="utf-8")
        log_file.write(f"\n[{self._utc_now_iso()}] launch: {' '.join(map(str, command))}\n")
        return subprocess.Popen(
            list(command),
            cwd=str(self.root),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=dict(env),
            **popen_kwargs,
        )

    def _launch_log_path(self, plan: StrategyLaunchPlan) -> str:
        return str(self._launch_log_path_for_plan(plan).relative_to(self.root))

    def _launch_log_path_for_plan(self, plan: StrategyLaunchPlan) -> Path:
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        safe_strategy = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in plan.strategy_id)
        name = f"launcher_environment_{plan.environment}_{safe_strategy}_{stamp}.log"
        return self.root / "engine" / "logs" / name

    def _launch_log_path_from_command(self, command: Sequence[str]) -> Path:
        command_line = " ".join(str(item) for item in command)
        environment = self._command_arg(command_line, "--environment") or "unknown"
        strategy_id = self._command_arg(command_line, "--strategy-id") or self._command_arg(command_line, "--state-id") or "unknown"
        safe_strategy = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in strategy_id)
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        name = f"launcher_environment_{environment}_{safe_strategy}_{stamp}.log"
        return self.root / "engine" / "logs" / name

    @staticmethod
    def _python_bin() -> str:
        return os.environ.get("OKX_TRADING_SYSTEM_PYTHON") or sys.executable or "python3"

    def _okx_cli_env(self, profile: str) -> dict[str, str]:
        env = os.environ.copy()
        env["OKX_ENVIRONMENT_RUNNER"] = "1"
        if os.name == "nt" and not env.get("OKX_CLI_BIN"):
            appdata = env.get("APPDATA")
            if appdata:
                candidate = Path(appdata) / "npm" / "okx.cmd"
                if candidate.exists():
                    env["OKX_CLI_BIN"] = str(candidate)
            if not env.get("OKX_CLI_BIN"):
                found = shutil.which("okx.cmd")
                if found:
                    env["OKX_CLI_BIN"] = found
        if profile == "live":
            env["LIVE_TRADING"] = "true"
        if profile != "live":
            for key in OKX_ENV_CREDENTIAL_KEYS:
                env.pop(key, None)
        return env

    @staticmethod
    def _command_arg(command: str, key: str) -> str | None:
        parts = command.split()
        for idx, part in enumerate(parts):
            if part == key and idx + 1 < len(parts):
                return parts[idx + 1]
            if part.startswith(f"{key}="):
                return part.split("=", 1)[1]
        return None

    @staticmethod
    def _validate_environment(environment: str) -> None:
        if environment not in ALLOWED_ENVIRONMENTS:
            raise ValueError(f"unsupported runtime environment: {environment}")
