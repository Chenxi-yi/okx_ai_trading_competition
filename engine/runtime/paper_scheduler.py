"""Production-style scheduler wrapper for PaperRunner."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Mapping

from runtime.paper_runner import PaperRunner


@dataclass(frozen=True)
class PaperSchedulerConfig:
    interval_sec: float = 60.0
    max_cycles: int | None = None
    stop_path: Path | str | None = Path("engine/control/paper.stop")
    status_path: Path | str | None = Path("engine/logs/paper_scheduler_status.json")
    max_consecutive_errors: int = 5


class PaperScheduler:
    """Runs PaperRunner in a controlled loop with heartbeat and error status."""

    def __init__(self, runner: PaperRunner, config: PaperSchedulerConfig | None = None):
        self.runner = runner
        self.config = config or PaperSchedulerConfig()
        self.cycles = 0
        self.consecutive_errors = 0
        self.last_status: dict[str, object] = {}

    def run(self) -> dict[str, object]:
        while True:
            if self._stop_requested():
                status = self._status("stopped", {"reason": "stop file present"})
                self._write_status(status)
                return status
            if self.config.max_cycles is not None and self.cycles >= self.config.max_cycles:
                status = self._status("completed", {"reason": "max_cycles reached"})
                self._write_status(status)
                return status

            started = datetime.now(timezone.utc)
            try:
                runner_status = self.runner.run_once()
                self.consecutive_errors = 0
                self.cycles += 1
                status = self._status(
                    "ok",
                    {
                        "cycle_started_at": started.isoformat(),
                        "runner": runner_status,
                    },
                )
                self._write_status(status)
            except Exception as exc:
                self.consecutive_errors += 1
                self.cycles += 1
                status = self._status(
                    "error",
                    {
                        "cycle_started_at": started.isoformat(),
                        "error": str(exc),
                        "error_type": exc.__class__.__name__,
                    },
                )
                self._write_status(status)
                if self.consecutive_errors >= self.config.max_consecutive_errors:
                    halted = self._status("halted", {"reason": "max_consecutive_errors reached", "last_error": str(exc)})
                    self._write_status(halted)
                    return halted

            if self.config.max_cycles is not None and self.cycles >= self.config.max_cycles:
                continue
            time.sleep(max(0.0, self.config.interval_sec))

    def _status(self, scheduler_status: str, extra: Mapping[str, object]) -> dict[str, object]:
        status = {
            "scheduler_status": scheduler_status,
            "heartbeat_at": datetime.now(timezone.utc).isoformat(),
            "cycles": self.cycles,
            "consecutive_errors": self.consecutive_errors,
            "interval_sec": self.config.interval_sec,
        }
        status.update(dict(extra))
        self.last_status = status
        return status

    def _stop_requested(self) -> bool:
        return bool(self.config.stop_path and Path(self.config.stop_path).exists())

    def _write_status(self, status: Mapping[str, object]) -> None:
        if self.config.status_path is None:
            return
        path = Path(self.config.status_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(status), indent=2, sort_keys=True))
