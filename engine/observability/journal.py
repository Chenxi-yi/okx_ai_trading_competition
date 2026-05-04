"""Append-only decision journal."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

from contracts import DecisionEvent, RiskEvent


class DecisionJournal:
    """Writes decision, risk, order, fill, and outcome events as JSONL."""

    def __init__(self, base_dir: Path | str):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def append_decision(self, event: DecisionEvent) -> Path:
        return self._append("decisions.jsonl", event)

    def append_risk(self, event: RiskEvent) -> Path:
        return self._append("risk_events.jsonl", event)

    def append_raw(self, file_name: str, payload: Mapping[str, Any]) -> Path:
        return self._append(file_name, dict(payload))

    def _append(self, file_name: str, payload: Any) -> Path:
        path = self.base_dir / file_name
        with path.open("a") as f:
            f.write(json.dumps(self._to_jsonable(payload), sort_keys=True) + "\n")
        return path

    def _to_jsonable(self, value: Any) -> Any:
        if is_dataclass(value):
            return self._to_jsonable(asdict(value))
        if isinstance(value, dict):
            return {str(k): self._to_jsonable(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._to_jsonable(v) for v in value]
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        return value
