"""JSON-backed Strategy Office registry."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from config.settings import BASE_DIR
from .schemas import (
    ParameterSet,
    PerformanceRecord,
    PromotionRecord,
    StrategyRecord,
    StrategyStatus,
    utc_now,
)


DEFAULT_REGISTRY_PATH = BASE_DIR / "config" / "strategy_registry.json"
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "idea": {"research", "retired"},
    "research": {"backtest", "paused", "retired"},
    "backtest": {"paper", "research", "paused", "retired"},
    "paper": {"live", "backtest", "paused", "retired"},
    "live": {"paused", "retired"},
    "paused": {"research", "backtest", "paper", "live", "retired"},
    "retired": set(),
}


class StrategyRegistry:
    """Stores strategy identity, parameters, performance, and lifecycle."""

    def __init__(self, path: Path | str = DEFAULT_REGISTRY_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write(self._empty())

    def list_strategies(self, status: str | None = None, book: str | None = None) -> list[StrategyRecord]:
        data = self._read()
        records = [StrategyRecord.from_dict(item) for item in data.get("strategies", [])]
        if status:
            records = [record for record in records if record.status == status]
        if book:
            records = [record for record in records if record.book == book]
        return sorted(records, key=lambda r: r.strategy_id)

    def get_strategy(self, strategy_id: str) -> StrategyRecord:
        for record in self.list_strategies():
            if record.strategy_id == strategy_id:
                return record
        raise KeyError(f"Unknown strategy_id: {strategy_id}")

    def upsert_strategy(self, record: StrategyRecord) -> StrategyRecord:
        data = self._read()
        updated = replace(record, updated_at=utc_now())
        rows = [item for item in data.get("strategies", []) if item.get("strategy_id") != record.strategy_id]
        rows.append(updated.to_dict())
        data["strategies"] = sorted(rows, key=lambda item: item["strategy_id"])
        self._write(data)
        return updated

    def add_parameter_set(self, params: ParameterSet, make_default: bool = False) -> ParameterSet:
        data = self._read()
        if params.strategy_id not in {item["strategy_id"] for item in data.get("strategies", [])}:
            raise KeyError(f"Cannot add params for unknown strategy: {params.strategy_id}")
        rows = [
            item for item in data.get("parameter_sets", [])
            if item.get("parameter_set_id") != params.parameter_set_id
        ]
        rows.append(params.to_dict())
        data["parameter_sets"] = sorted(rows, key=lambda item: item["parameter_set_id"])
        if make_default:
            data["strategies"] = [
                {
                    **item,
                    "default_parameter_set_id": params.parameter_set_id,
                    "updated_at": utc_now(),
                }
                if item.get("strategy_id") == params.strategy_id else item
                for item in data.get("strategies", [])
            ]
        self._write(data)
        return params

    def get_parameter_set(self, parameter_set_id: str) -> ParameterSet:
        for item in self._read().get("parameter_sets", []):
            if item.get("parameter_set_id") == parameter_set_id:
                return ParameterSet.from_dict(item)
        raise KeyError(f"Unknown parameter_set_id: {parameter_set_id}")

    def parameter_sets_for(self, strategy_id: str) -> list[ParameterSet]:
        return [
            ParameterSet.from_dict(item)
            for item in self._read().get("parameter_sets", [])
            if item.get("strategy_id") == strategy_id
        ]

    def add_performance(self, record: PerformanceRecord) -> PerformanceRecord:
        self.get_strategy(record.strategy_id)
        self.get_parameter_set(record.parameter_set_id)
        data = self._read()
        rows = [item for item in data.get("performance", []) if item.get("record_id") != record.record_id]
        rows.append(record.to_dict())
        data["performance"] = rows
        self._write(data)
        return record

    def performance_for(self, strategy_id: str, mode: str | None = None) -> list[PerformanceRecord]:
        rows = [
            PerformanceRecord.from_dict(item)
            for item in self._read().get("performance", [])
            if item.get("strategy_id") == strategy_id
        ]
        if mode:
            rows = [row for row in rows if row.mode == mode]
        return sorted(rows, key=lambda row: row.created_at)

    def promote(
        self,
        strategy_id: str,
        to_status: StrategyStatus,
        reason: str,
        evidence_record_ids: Iterable[str] = (),
        approved_by: str = "personal",
    ) -> PromotionRecord:
        current = self.get_strategy(strategy_id)
        allowed = ALLOWED_TRANSITIONS.get(current.status, set())
        if to_status not in allowed:
            raise ValueError(f"Invalid status transition: {current.status} -> {to_status}")
        promotion = PromotionRecord(
            promotion_id=f"promo-{uuid4()}",
            strategy_id=strategy_id,
            from_status=current.status,
            to_status=to_status,
            reason=reason,
            evidence_record_ids=tuple(evidence_record_ids),
            approved_by=approved_by,
        )
        data = self._read()
        data["promotions"] = [*data.get("promotions", []), promotion.to_dict()]
        data["strategies"] = [
            {
                **item,
                "status": to_status,
                "live_enabled": bool(item.get("live_enabled", False)) if to_status == "live" else False,
                "updated_at": utc_now(),
            }
            if item.get("strategy_id") == strategy_id else item
            for item in data.get("strategies", [])
        ]
        self._write(data)
        return promotion

    def set_live_allocation(self, strategy_id: str, allocation_pct: float, enabled: bool) -> StrategyRecord:
        current = self.get_strategy(strategy_id)
        if enabled and current.status != "live":
            raise ValueError(f"Cannot enable live allocation for non-live strategy {strategy_id}: {current.status}")
        updated = replace(
            current,
            live_enabled=enabled,
            live_allocation_pct=max(0.0, float(allocation_pct)),
            updated_at=utc_now(),
        )
        return self.upsert_strategy(updated)

    def _read(self) -> dict[str, Any]:
        try:
            with self.path.open() as f:
                return json.load(f)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Strategy registry is invalid JSON: {self.path}") from exc

    def _write(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, self.path)

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "version": "1.0",
            "created_at": utc_now(),
            "strategies": [],
            "parameter_sets": [],
            "performance": [],
            "promotions": [],
        }
