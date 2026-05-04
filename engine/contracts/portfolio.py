"""Portfolio-state contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Mapping

PositionSide = Literal["long", "short"]


@dataclass(frozen=True)
class Position:
    symbol: str
    inst_id: str
    side: PositionSide
    entry_price: float
    size_contracts: float
    opened_at: datetime
    decision_id: str
    target: float | None = None
    stop: float | None = None
    time_stop: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PortfolioState:
    timestamp: datetime
    nav_usdt: float
    free_usdt: float
    positions: Mapping[str, Position] = field(default_factory=dict)
    per_strategy_used: Mapping[str, float] = field(default_factory=dict)
    realized_pnl_usdt: float = 0.0
    unrealized_pnl_usdt: float = 0.0
    total_fees_usdt: float = 0.0
    risk_state: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def gross_position_count(self) -> int:
        return len(self.positions)
