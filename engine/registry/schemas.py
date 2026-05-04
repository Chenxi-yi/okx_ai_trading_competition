"""Dataclasses for the Strategy Office registry."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Mapping

StrategyBook = Literal["core", "tactical", "speculative"]
StrategyStatus = Literal["idea", "research", "backtest", "paper", "live", "paused", "retired"]
PerformanceMode = Literal["backtest", "paper", "live", "stress", "monte_carlo"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class RiskBudget:
    max_nav_pct: float
    max_drawdown_pct: float
    max_leverage: float
    max_daily_loss_pct: float | None = None
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RiskBudget":
        return cls(
            max_nav_pct=float(data.get("max_nav_pct", 0.0)),
            max_drawdown_pct=float(data.get("max_drawdown_pct", 0.0)),
            max_leverage=float(data.get("max_leverage", 1.0)),
            max_daily_loss_pct=(
                float(data["max_daily_loss_pct"]) if data.get("max_daily_loss_pct") is not None else None
            ),
            notes=str(data.get("notes", "")),
        )


@dataclass(frozen=True)
class StrategyRecord:
    strategy_id: str
    name: str
    book: StrategyBook
    status: StrategyStatus
    version: str
    module: str
    class_name: str
    default_parameter_set_id: str | None
    risk_budget: RiskBudget
    live_enabled: bool = False
    live_allocation_pct: float = 0.0
    description: str = ""
    owner: str = "personal"
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    tags: tuple[str, ...] = ()
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["risk_budget"] = self.risk_budget.to_dict()
        data["tags"] = list(self.tags)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "StrategyRecord":
        return cls(
            strategy_id=str(data["strategy_id"]),
            name=str(data.get("name", data["strategy_id"])),
            book=data.get("book", "core"),  # type: ignore[arg-type]
            status=data.get("status", "idea"),  # type: ignore[arg-type]
            version=str(data.get("version", "0.1.0")),
            module=str(data.get("module", "")),
            class_name=str(data.get("class_name", "")),
            default_parameter_set_id=(
                str(data["default_parameter_set_id"]) if data.get("default_parameter_set_id") else None
            ),
            risk_budget=RiskBudget.from_dict(data.get("risk_budget", {})),
            live_enabled=bool(data.get("live_enabled", False)),
            live_allocation_pct=float(data.get("live_allocation_pct", 0.0)),
            description=str(data.get("description", "")),
            owner=str(data.get("owner", "personal")),
            created_at=str(data.get("created_at", utc_now())),
            updated_at=str(data.get("updated_at", utc_now())),
            tags=tuple(data.get("tags", ())),
            notes=str(data.get("notes", "")),
        )


@dataclass(frozen=True)
class ParameterSet:
    parameter_set_id: str
    strategy_id: str
    version: str
    params: Mapping[str, Any]
    created_at: str = field(default_factory=utc_now)
    notes: str = ""
    parent_parameter_set_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "parameter_set_id": self.parameter_set_id,
            "strategy_id": self.strategy_id,
            "version": self.version,
            "params": dict(self.params),
            "created_at": self.created_at,
            "notes": self.notes,
            "parent_parameter_set_id": self.parent_parameter_set_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ParameterSet":
        return cls(
            parameter_set_id=str(data["parameter_set_id"]),
            strategy_id=str(data["strategy_id"]),
            version=str(data.get("version", "1")),
            params=dict(data.get("params", {})),
            created_at=str(data.get("created_at", utc_now())),
            notes=str(data.get("notes", "")),
            parent_parameter_set_id=(
                str(data["parent_parameter_set_id"]) if data.get("parent_parameter_set_id") else None
            ),
        )


@dataclass(frozen=True)
class PerformanceRecord:
    record_id: str
    strategy_id: str
    parameter_set_id: str
    mode: PerformanceMode
    start: str
    end: str
    metrics: Mapping[str, Any]
    costs: Mapping[str, Any] = field(default_factory=dict)
    dataset_id: str | None = None
    decision_journal_path: str | None = None
    created_at: str = field(default_factory=utc_now)
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "strategy_id": self.strategy_id,
            "parameter_set_id": self.parameter_set_id,
            "mode": self.mode,
            "start": self.start,
            "end": self.end,
            "metrics": dict(self.metrics),
            "costs": dict(self.costs),
            "dataset_id": self.dataset_id,
            "decision_journal_path": self.decision_journal_path,
            "created_at": self.created_at,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PerformanceRecord":
        return cls(
            record_id=str(data["record_id"]),
            strategy_id=str(data["strategy_id"]),
            parameter_set_id=str(data["parameter_set_id"]),
            mode=data.get("mode", "backtest"),  # type: ignore[arg-type]
            start=str(data.get("start", "")),
            end=str(data.get("end", "")),
            metrics=dict(data.get("metrics", {})),
            costs=dict(data.get("costs", {})),
            dataset_id=str(data["dataset_id"]) if data.get("dataset_id") else None,
            decision_journal_path=(
                str(data["decision_journal_path"]) if data.get("decision_journal_path") else None
            ),
            created_at=str(data.get("created_at", utc_now())),
            notes=str(data.get("notes", "")),
        )


@dataclass(frozen=True)
class PromotionRecord:
    promotion_id: str
    strategy_id: str
    from_status: StrategyStatus
    to_status: StrategyStatus
    reason: str
    evidence_record_ids: tuple[str, ...] = ()
    created_at: str = field(default_factory=utc_now)
    approved_by: str = "personal"

    def to_dict(self) -> dict[str, Any]:
        return {
            "promotion_id": self.promotion_id,
            "strategy_id": self.strategy_id,
            "from_status": self.from_status,
            "to_status": self.to_status,
            "reason": self.reason,
            "evidence_record_ids": list(self.evidence_record_ids),
            "created_at": self.created_at,
            "approved_by": self.approved_by,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PromotionRecord":
        return cls(
            promotion_id=str(data["promotion_id"]),
            strategy_id=str(data["strategy_id"]),
            from_status=data.get("from_status", "idea"),  # type: ignore[arg-type]
            to_status=data.get("to_status", "research"),  # type: ignore[arg-type]
            reason=str(data.get("reason", "")),
            evidence_record_ids=tuple(data.get("evidence_record_ids", ())),
            created_at=str(data.get("created_at", utc_now())),
            approved_by=str(data.get("approved_by", "personal")),
        )
