"""Schemas for the local OKX Agent Trade Kit boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping


@dataclass(frozen=True)
class KitCommand:
    module: str
    action: str
    args: tuple[str, ...] = ()
    profile: str = "demo"
    json_output: bool = True
    allow_live: bool = False
    timeout_sec: float = 30.0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_trade(self) -> bool:
        return self.module in {"swap", "spot", "futures", "option", "bot"} and self.action not in {
            "positions",
            "orders",
            "get",
            "fills",
            "get-leverage",
        }


@dataclass(frozen=True)
class KitResult:
    command: KitCommand
    argv: tuple[str, ...]
    ok: bool
    returncode: int
    stdout: str
    stderr: str
    data: Any = None
    error: str | None = None
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finished_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def require_ok(self) -> "KitResult":
        if not self.ok:
            raise RuntimeError(self.error or self.stderr or self.stdout or "OKX Agent Trade Kit command failed")
        return self
