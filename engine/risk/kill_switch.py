"""System kill-switch state."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class KillSwitchState:
    active: bool
    reason: str = ""


class KillSwitch:
    def __init__(self, path: Path | str = "engine/control/kill.switch"):
        self.path = Path(path)

    def state(self) -> KillSwitchState:
        if self.path.exists():
            reason = self.path.read_text().strip() if self.path.is_file() else "kill switch present"
            return KillSwitchState(True, reason or "kill switch present")
        return KillSwitchState(False, "")
