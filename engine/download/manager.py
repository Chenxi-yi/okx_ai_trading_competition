"""Manage long-running market data download jobs.

The downloader scripts already write durable manifests, status heartbeats, and
progress JSONL files. This manager keeps the control plane thin: it starts the
existing scripts, summarizes progress, and pauses by terminating the matching
process. Resume is just a restart with the same run id because completed jobs are
skipped by the downloader.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .quality import build_training_history_quality, write_quality_summary


ROOT_DIR = Path(__file__).resolve().parents[2]
ENGINE_DIR = ROOT_DIR / "engine"
TRAINING_HISTORY_DIR = ENGINE_DIR / "data" / "training_history"
DERIVATIVES_STRUCTURE_DIR = ENGINE_DIR / "data" / "derivatives_structure"
LOG_DIR = ENGINE_DIR / "logs" / "download"


@dataclass(frozen=True)
class DownloadStartRequest:
    dataset_type: str = "training_history"
    run_id: str | None = None
    symbols: str | None = None
    symbols_manifest: str | None = None
    min_volume_usd: float = 30_000_000.0
    max_symbols: int = 250
    start: str = "2024-01-01"
    end: str | None = None
    timeframes: str = "1h"
    sleep_sec: float = 0.5
    retry_attempts: int = 4
    retry_sleep_sec: float = 8.0
    min_coverage: float = 0.8
    min_rows: int = 100
    refresh_universe: bool = False
    discover_only: bool = False

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "DownloadStartRequest":
        return cls(
            dataset_type=str(payload.get("dataset_type") or payload.get("type") or "training_history"),
            run_id=_clean_optional_str(payload.get("run_id")),
            symbols=_clean_optional_str(payload.get("symbols")),
            symbols_manifest=_clean_optional_str(payload.get("symbols_manifest")),
            min_volume_usd=float(payload.get("min_volume_usd", 30_000_000.0)),
            max_symbols=int(payload.get("max_symbols", 250)),
            start=str(payload.get("start") or "2024-01-01"),
            end=_clean_optional_str(payload.get("end")),
            timeframes=str(payload.get("timeframes") or "1h"),
            sleep_sec=float(payload.get("sleep_sec", 0.5)),
            retry_attempts=int(payload.get("retry_attempts", 4)),
            retry_sleep_sec=float(payload.get("retry_sleep_sec", 8.0)),
            min_coverage=float(payload.get("min_coverage", 0.8)),
            min_rows=int(payload.get("min_rows", 100)),
            refresh_universe=bool(payload.get("refresh_universe", False)),
            discover_only=bool(payload.get("discover_only", False)),
        )

    def normalized_run_id(self) -> str:
        if self.run_id:
            return self.run_id
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        prefix = "train_hist" if self.dataset_type == "training_history" else "data"
        return f"{prefix}_{ts}"


class DataDownloadManager:
    def __init__(self, root_dir: Path = ROOT_DIR) -> None:
        self.root_dir = root_dir
        self.engine_dir = root_dir / "engine"
        self.training_history_dir = self.engine_dir / "data" / "training_history"
        self.derivatives_structure_dir = self.engine_dir / "data" / "derivatives_structure"
        self.log_dir = self.engine_dir / "logs" / "download"

    def status(self, run_id: str | None = None) -> dict[str, Any]:
        selected_run_id = run_id or self.latest_run_id()
        if not selected_run_id:
            return {"ok": True, "available": False, "message": "no download runs found"}

        run_dir = self.run_dir(selected_run_id) or (self.training_history_dir / selected_run_id)
        manifest = read_json(run_dir / "manifest.json") or {}
        heartbeat = read_json(run_dir / "status.json") or {}
        progress = iter_jsonl(run_dir / "progress.jsonl")
        dataset_type = self._dataset_type(manifest, run_dir)
        timeframes = manifest.get("timeframes") or [manifest.get("timeframe") or "1h"]
        kinds = manifest.get("kinds") or ["ohlcv"]
        symbols = manifest.get("symbols") or []
        total_jobs = int(manifest.get("summary", {}).get("total_jobs") or (len(symbols) * len(timeframes) * len(kinds)))

        latest_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
        for record in progress:
            key = (
                str(record.get("symbol", "")),
                str(record.get("kind", "ohlcv")),
                str(record.get("timeframe", manifest.get("timeframe", ""))),
            )
            latest_by_key[key] = record

        ok_count = sum(1 for record in latest_by_key.values() if record.get("status") == "ok")
        failed_count = sum(1 for record in latest_by_key.values() if record.get("status") == "failed")
        attempted_count = len(latest_by_key)
        last_record = progress[-1] if progress else None
        processes = self.find_processes(selected_run_id)
        current_symbol = heartbeat.get("current_symbol") or heartbeat.get("last_symbol")
        current_timeframe = heartbeat.get("current_timeframe") or heartbeat.get("last_timeframe")
        current_kind = heartbeat.get("current_kind") or heartbeat.get("last_kind")
        next_symbol, next_timeframe, next_kind = self._next_job(symbols, timeframes, kinds, latest_by_key)
        percent = round((ok_count / total_jobs) * 100, 2) if total_jobs else 0.0
        quality = self.ensure_quality_summary(run_dir, manifest, progress) if manifest and not processes else None
        catalog_record = None
        if quality and dataset_type == "training_history" and quality.get("validation_status") in {"ok", "warn"}:
            catalog_record = self._compact_catalog_record(self.register_completed_run(selected_run_id, run_dir, manifest, quality))

        return {
            "ok": True,
            "available": True,
            "dataset_type": dataset_type,
            "run_id": selected_run_id,
            "run_dir": self._relpath(run_dir),
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
            "current_timeframe": current_timeframe,
            "current_kind": current_kind,
            "next_symbol": next_symbol,
            "next_timeframe": next_timeframe,
            "next_kind": next_kind,
            "last_record": last_record,
            "updated_at": heartbeat.get("updated_at") or (last_record or {}).get("ts") or manifest.get("created_at"),
            "progress_tail": progress[-30:],
            "quality": quality,
            "catalog_record": catalog_record,
        }

    def list_runs(self) -> dict[str, Any]:
        runs: list[dict[str, Any]] = []
        for root in self.download_roots():
            if not root.exists():
                continue
            for path in root.iterdir():
                manifest_path = path / "manifest.json"
                if not path.is_dir() or not manifest_path.exists():
                    continue
                manifest = read_json(manifest_path) or {}
                status = read_json(path / "status.json") or {}
                run_id = manifest.get("run_id") or path.name
                runs.append(
                    {
                        "run_id": run_id,
                        "dataset_type": self._dataset_type(manifest, path),
                        "run_dir": self._relpath(path),
                        "status": status.get("status") or manifest.get("status"),
                        "created_at": manifest.get("created_at"),
                        "updated_at": status.get("updated_at") or manifest.get("finished_at") or manifest.get("created_at"),
                        "total_jobs": manifest.get("summary", {}).get("total_jobs"),
                        "catalog_registered": self._catalog_has_run(str(run_id)),
                        "quality_status": (read_json(path / "quality_summary.json") or {}).get("validation_status"),
                        "running": bool(self.find_processes(str(run_id))),
                    }
                )
        runs.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
        return {"ok": True, "runs": runs}

    def start(self, request: DownloadStartRequest) -> dict[str, Any]:
        if request.dataset_type != "training_history":
            raise ValueError("only training_history downloads are supported by this module for now")
        run_id = request.normalized_run_id()
        existing = self.find_processes(run_id)
        if existing:
            return {"ok": True, "already_running": True, "run_id": run_id, "processes": existing, "status": self.status(run_id)}
        cmd = self._training_history_command(request, run_id)
        process = self._spawn(cmd, run_id)
        return {"ok": True, "run_id": run_id, "pid": process.pid, "command": cmd, "status": self.status(run_id)}

    def resume(self, run_id: str | None = None) -> dict[str, Any]:
        selected_run_id = run_id or self.latest_run_id()
        if not selected_run_id:
            raise ValueError("no download run found to resume")
        existing = self.find_processes(selected_run_id)
        if existing:
            return {
                "ok": True,
                "already_running": True,
                "run_id": selected_run_id,
                "processes": existing,
                "status": self.status(selected_run_id),
            }

        run_dir = self.run_dir(selected_run_id)
        if not run_dir:
            raise ValueError(f"download manifest not found for run_id={selected_run_id}")
        manifest = read_json(run_dir / "manifest.json") or {}
        if self._dataset_type(manifest, run_dir) != "training_history":
            raise ValueError("resume currently supports training_history runs only")
        request = DownloadStartRequest(
            dataset_type="training_history",
            run_id=selected_run_id,
            symbols_manifest=manifest.get("source_manifest"),
            min_volume_usd=float(manifest.get("min_volume_usd", 30_000_000.0)),
            max_symbols=int(manifest.get("max_symbols", len(manifest.get("symbols") or []) or 250)),
            start=str(manifest.get("start") or "2024-01-01"),
            end=str(manifest.get("end") or datetime.now(timezone.utc).strftime("%Y-%m-%d")),
            timeframes=",".join(manifest.get("timeframes") or ["1h"]),
            sleep_sec=0.5,
            retry_attempts=4,
            retry_sleep_sec=8.0,
            min_rows=100,
        )
        cmd = self._training_history_command(request, selected_run_id)
        process = self._spawn(cmd, selected_run_id)
        return {"ok": True, "run_id": selected_run_id, "pid": process.pid, "command": cmd, "status": self.status(selected_run_id)}

    def pause(self, run_id: str | None = None) -> dict[str, Any]:
        selected_run_id = run_id or self.latest_run_id()
        processes = self.find_processes(selected_run_id)
        stopped: list[int] = []
        for proc in processes:
            pid = int(proc["pid"])
            try:
                os.kill(pid, signal.SIGTERM)
                stopped.append(pid)
            except OSError:
                continue
        return {"ok": True, "run_id": selected_run_id, "stopped_pids": stopped}

    def quality_summary(self, run_id: str | None = None, refresh: bool = False) -> dict[str, Any]:
        selected_run_id = run_id or self.latest_run_id()
        if not selected_run_id:
            raise ValueError("no download run found")
        run_dir = self.run_dir(selected_run_id)
        if not run_dir:
            raise ValueError(f"download manifest not found for run_id={selected_run_id}")
        manifest = read_json(run_dir / "manifest.json") or {}
        if not refresh:
            existing = read_json(run_dir / "quality_summary.json")
            if existing:
                return existing
        progress = iter_jsonl(run_dir / "progress.jsonl")
        return self.ensure_quality_summary(run_dir, manifest, progress)

    def register_completed_run(
        self,
        run_id: str,
        run_dir: Path,
        manifest: dict[str, Any],
        quality: dict[str, Any],
        dataset_id: str | None = None,
    ) -> dict[str, Any]:
        if self._dataset_type(manifest, run_dir) != "training_history":
            raise ValueError("catalog registration currently supports training_history runs only")
        dataset_id = dataset_id or self._dataset_id(run_id, manifest)
        try:
            from engine.data.catalog import DataCatalog
        except ModuleNotFoundError:
            from data.catalog import DataCatalog

        record = DataCatalog().register_raw_ohlcv_run(dataset_id, self._relpath(run_dir), manifest, quality)
        return record.to_dict()

    def ensure_quality_summary(self, run_dir: Path, manifest: dict[str, Any], progress: list[dict[str, Any]]) -> dict[str, Any]:
        dataset_type = self._dataset_type(manifest, run_dir)
        if dataset_type != "training_history":
            existing = read_json(run_dir / "quality_summary.json")
            return existing or {
                "run_id": manifest.get("run_id") or run_dir.name,
                "dataset_type": dataset_type,
                "validation_status": "unsupported",
            }
        quality = build_training_history_quality(run_dir, self.engine_dir / "data" / "cache", manifest, progress)
        write_quality_summary(run_dir, quality)
        return quality

    def latest_run_id(self) -> str | None:
        active = [proc.get("run_id") for proc in self.find_processes(None) if proc.get("run_id")]
        if active:
            return str(active[0])
        candidates: list[Path] = []
        for root in self.download_roots():
            if root.exists():
                candidates.extend(path for path in root.iterdir() if path.is_dir() and (path / "manifest.json").exists())
        if not candidates:
            return None
        latest = max(candidates, key=lambda path: (path / "manifest.json").stat().st_mtime)
        return latest.name

    def find_processes(self, run_id: str | None = None) -> list[dict[str, Any]]:
        try:
            proc = subprocess.run(
                ["ps", "-axo", "pid=,command="],
                cwd=str(self.root_dir),
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
            parsed_run_id = _run_id_from_command(command)
            if run_id and parsed_run_id != run_id and run_id not in command:
                continue
            matches.append({"pid": pid, "command": command, "run_id": parsed_run_id})
        return matches

    def run_dir(self, run_id: str) -> Path | None:
        for root in self.download_roots():
            candidate = root / run_id
            if (candidate / "manifest.json").exists():
                return candidate
        return None

    def download_roots(self) -> list[Path]:
        return [self.training_history_dir, self.derivatives_structure_dir]

    def _training_history_command(self, request: DownloadStartRequest, run_id: str) -> list[str]:
        end = request.end or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cmd = [
            "python3",
            "scripts/fetch_training_history.py",
            "--run-id",
            run_id,
            "--start",
            request.start,
            "--end",
            end,
            "--timeframes",
            request.timeframes,
            "--sleep-sec",
            str(request.sleep_sec),
            "--retry-attempts",
            str(request.retry_attempts),
            "--retry-sleep-sec",
            str(request.retry_sleep_sec),
            "--min-coverage",
            str(request.min_coverage),
            "--min-rows",
            str(request.min_rows),
        ]
        if request.symbols:
            cmd.extend(["--symbols", request.symbols])
        elif request.symbols_manifest:
            cmd.extend(["--symbols-manifest", self._resolve_path_arg(request.symbols_manifest)])
        else:
            cmd.extend(["--min-volume-usd", str(request.min_volume_usd), "--max-symbols", str(request.max_symbols)])
        if request.refresh_universe:
            cmd.append("--refresh-universe")
        if request.discover_only:
            cmd.append("--discover-only")
        return cmd

    def _spawn(self, cmd: list[str], run_id: str) -> subprocess.Popen[Any]:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log_prefix = self.log_dir / f"{run_id}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        stdout = (log_prefix.with_suffix(".out")).open("ab")
        stderr = (log_prefix.with_suffix(".err")).open("ab")
        return subprocess.Popen(
            cmd,
            cwd=str(self.root_dir),
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )

    def _resolve_path_arg(self, value: str) -> str:
        path = Path(value)
        if path.is_absolute():
            return str(path)
        return str(self.root_dir / path)

    def _relpath(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.root_dir.resolve()))
        except ValueError:
            return str(path)

    def _dataset_type(self, manifest: dict[str, Any], run_dir: Path) -> str:
        if manifest.get("download_type"):
            return str(manifest["download_type"])
        if self.derivatives_structure_dir in run_dir.parents:
            return "derivatives_structure"
        return "training_history"

    def _dataset_id(self, run_id: str, manifest: dict[str, Any]) -> str:
        timeframes = "_".join(str(item) for item in manifest.get("timeframes") or [manifest.get("timeframe") or ""])
        suffix = f"_{timeframes}" if timeframes else ""
        return f"raw_ohlcv_{run_id}{suffix}"

    def _catalog_has_run(self, run_id: str) -> bool:
        try:
            from engine.data.catalog import DataCatalog
        except ModuleNotFoundError:
            from data.catalog import DataCatalog
        try:
            records = DataCatalog().list(kind="raw_ohlcv")
        except Exception:
            return False
        return any(record.metadata.get("run_id") == run_id for record in records)

    def _compact_catalog_record(self, record: dict[str, Any]) -> dict[str, Any]:
        return {
            "dataset_id": record.get("dataset_id"),
            "kind": record.get("kind"),
            "source": record.get("source"),
            "path": record.get("path"),
            "timeframe": record.get("timeframe"),
            "rows": record.get("rows"),
            "status": record.get("status"),
            "updated_at": record.get("updated_at"),
        }

    def _next_job(
        self,
        symbols: list[str],
        timeframes: list[str],
        kinds: list[str],
        latest_by_key: dict[tuple[str, str, str], dict[str, Any]],
    ) -> tuple[str | None, str | None, str | None]:
        done_ok = {key for key, record in latest_by_key.items() if record.get("status") == "ok"}
        for timeframe in timeframes:
            for symbol in symbols:
                for kind in kinds:
                    if (str(symbol), str(kind), str(timeframe)) not in done_ok:
                        return str(symbol), str(timeframe), str(kind)
        return None, None, None


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


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _run_id_from_command(command: str) -> str | None:
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    for i, part in enumerate(parts):
        if part == "--run-id" and i + 1 < len(parts):
            return parts[i + 1]
        if part.startswith("--run-id="):
            return part.split("=", 1)[1]
    return None


def _clean_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None
