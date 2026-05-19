"""Thin subprocess client for OKX Agent Trade Kit CLI."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from .schemas import KitCommand, KitResult


Runner = Callable[..., subprocess.CompletedProcess[str]]
OKX_ENV_CREDENTIAL_KEYS = {
    "OKX_API_KEY",
    "OKX_SECRET_KEY",
    "OKX_API_SECRET",
    "OKX_PASSPHRASE",
}


@dataclass(frozen=True)
class KitClientConfig:
    binary: str = "okx"
    default_profile: str = "demo"
    default_timeout_sec: float = 30.0
    audit_path: Path | str | None = Path("engine/logs/kit/audit.jsonl")
    live_enabled: bool = False


class KitClient:
    """Local, token-free boundary to the OKX Agent Trade Kit CLI."""

    def __init__(self, config: KitClientConfig | None = None, runner: Runner | None = None):
        self.config = config or KitClientConfig()
        self.runner = runner or subprocess.run

    def run(self, command: KitCommand) -> KitResult:
        if command.profile == "live" and command.is_trade and not (command.allow_live and self.config.live_enabled):
            raise PermissionError("live trading through KitClient requires allow_live=True and live_enabled=True")

        argv = self.build_argv(command)
        started_at = datetime.now(timezone.utc).isoformat()
        try:
            completed = self.runner(
                list(argv),
                capture_output=True,
                text=True,
                timeout=command.timeout_sec or self.config.default_timeout_sec,
                env=_command_env(command.profile or self.config.default_profile),
            )
            stdout = completed.stdout.strip() if completed.stdout else ""
            stderr = completed.stderr.strip() if completed.stderr else ""
            data = _parse_json(stdout) if command.json_output else None
            result = KitResult(
                command=command,
                argv=argv,
                ok=completed.returncode == 0,
                returncode=completed.returncode,
                stdout=stdout,
                stderr=stderr,
                data=data,
                error=None if completed.returncode == 0 else (stderr or stdout),
                started_at=started_at,
                finished_at=datetime.now(timezone.utc).isoformat(),
            )
        except FileNotFoundError as exc:
            result = KitResult(
                command=command,
                argv=argv,
                ok=False,
                returncode=127,
                stdout="",
                stderr=str(exc),
                error="OKX Agent Trade Kit CLI not found. Install @okx_ai/okx-trade-cli.",
                started_at=started_at,
                finished_at=datetime.now(timezone.utc).isoformat(),
            )
        except subprocess.TimeoutExpired as exc:
            result = KitResult(
                command=command,
                argv=argv,
                ok=False,
                returncode=124,
                stdout=(exc.stdout or "").strip() if isinstance(exc.stdout, str) else "",
                stderr=(exc.stderr or "").strip() if isinstance(exc.stderr, str) else "",
                error=f"OKX Agent Trade Kit command timed out after {command.timeout_sec}s",
                started_at=started_at,
                finished_at=datetime.now(timezone.utc).isoformat(),
            )
        self._audit(result)
        return result

    def build_argv(self, command: KitCommand) -> tuple[str, ...]:
        profile = command.profile or self.config.default_profile
        argv: list[str] = [self.config.binary, "--profile", profile]
        if command.json_output:
            argv.append("--json")
        argv.extend([command.module, command.action])
        argv.extend(command.args)
        return tuple(argv)

    def _audit(self, result: KitResult) -> None:
        if self.config.audit_path is None:
            return
        path = Path(self.config.audit_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts": result.finished_at,
            "ok": result.ok,
            "returncode": result.returncode,
            "profile": result.command.profile,
            "module": result.command.module,
            "action": result.command.action,
            "argv": list(_redact_argv(result.argv)),
            "error": result.error,
            "metadata": dict(result.command.metadata),
        }
        with path.open("a") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")


def _parse_json(raw: str) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _redact_argv(argv: Sequence[str]) -> tuple[str, ...]:
    # Current Kit commands do not pass secrets on argv, but keep one place for
    # future redaction if setup/config commands are added.
    return tuple(argv)


def _command_env(profile: str | None) -> dict[str, str]:
    env = os.environ.copy()
    if profile and profile != "live":
        for key in OKX_ENV_CREDENTIAL_KEYS:
            env.pop(key, None)
    return env
