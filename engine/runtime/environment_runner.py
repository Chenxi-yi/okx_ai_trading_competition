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
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence

try:
    from config.settings import BASE_DIR
    from registry import StrategyRecord, StrategyRegistry
except ModuleNotFoundError:
    from engine.config.settings import BASE_DIR
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


@dataclass(frozen=True)
class EnvironmentRunResult:
    environment: str
    plans: tuple[StrategyLaunchPlan, ...]
    results: tuple[StrategyLaunchResult, ...]

    @property
    def ok(self) -> bool:
        return all(item.error is None for item in self.results)


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

    def status(self, environment: str, *, plans: Sequence[StrategyLaunchPlan] | None = None) -> dict[str, object]:
        """Return structured runner truth for control-plane consumers."""
        self._validate_environment(environment)
        selected_plans = tuple(plans) if plans is not None else self.plan(environment)
        rows = [self._status_row(plan) for plan in selected_plans]
        blocked = [row for row in rows if not row.get("readiness", {}).get("ok")]
        running = [row for row in rows if row.get("running")]
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
                "python3",
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
                "python3",
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
            proc = self.process_starter(plan.command, self._okx_cli_env(plan.okx_profile))
            return StrategyLaunchResult(plan=plan, started=True, pid=int(proc.pid))
        except Exception as exc:
            return StrategyLaunchResult(plan=plan, started=False, error=str(exc))

    def _status_row(self, plan: StrategyLaunchPlan) -> dict[str, object]:
        processes = [dict(item) for item in self.existing_processes(plan)]
        scheduler = self._scheduler_snapshot(plan)
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
        }

    def _scheduler_snapshot(self, plan: StrategyLaunchPlan) -> dict[str, object]:
        return self._read_json(self._state_file(plan, suffix="_scheduler.json"))

    def _state_snapshot(self, plan: StrategyLaunchPlan) -> dict[str, object]:
        return self._read_json(self._state_file(plan, suffix=".json"))

    def _state_file(self, plan: StrategyLaunchPlan, *, suffix: str) -> Path:
        if plan.runner == "c_auto_v2_micro_live":
            prefix = f"{plan.state_id}_{plan.environment}"
            return self.root / "engine" / "logs" / "c_auto_v2_micro_live" / f"{prefix}{suffix}"
        prefix = f"{plan.state_id}_{plan.environment}"
        return self.root / "engine" / "logs" / "research_sleeves" / f"{prefix}{suffix}"

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

    def _process_from_exclusive_lock(self, plan: StrategyLaunchPlan) -> Mapping[str, object] | None:
        for strategy_id in self._lock_strategy_ids(plan):
            safe_strategy = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in strategy_id)
            path = self.root / "engine" / "control" / f"exclusive_strategy_{safe_strategy}.lock"
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
                "lock_strategy_id": strategy_id,
                "source": "exclusive_strategy_lock",
                "lock_path": str(path),
            }
        return None

    @staticmethod
    def _lock_strategy_ids(plan: StrategyLaunchPlan) -> tuple[str, ...]:
        raw = plan.metadata.get("lock_strategy_id") or plan.metadata.get("exclusive_strategy_id")
        values = [plan.strategy_id]
        if raw:
            values.append(str(raw))
        return tuple(dict.fromkeys(values))

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except OSError as exc:
            if getattr(exc, "errno", None) == errno.EPERM:
                return True
            return False

    def _default_process_starter(self, command: Sequence[str], env: Mapping[str, str]) -> subprocess.Popen:
        return subprocess.Popen(
            list(command),
            cwd=str(self.root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=dict(env),
        )

    def _okx_cli_env(self, profile: str) -> dict[str, str]:
        env = os.environ.copy()
        env["OKX_ENVIRONMENT_RUNNER"] = "1"
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
