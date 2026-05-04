"""Universe snapshot helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class UniverseSnapshot:
    universe_id: str
    symbols: tuple[str, ...]
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    filters: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "universe_id": self.universe_id,
            "symbols": list(self.symbols),
            "created_at": self.created_at,
            "filters": dict(self.filters),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "UniverseSnapshot":
        return cls(
            universe_id=str(data["universe_id"]),
            symbols=tuple(data.get("symbols", ())),
            created_at=str(data.get("created_at", datetime.now(timezone.utc).isoformat())),
            filters=dict(data.get("filters", {})),
            metadata=dict(data.get("metadata", {})),
        )


def write_universe_snapshot(snapshot: UniverseSnapshot, path: Path | str) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snapshot.to_dict(), indent=2, sort_keys=True))


def read_universe_snapshot(path: Path | str) -> UniverseSnapshot:
    return UniverseSnapshot.from_dict(json.loads(Path(path).read_text()))
