"""Position lifecycle intent contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Mapping

PositionAction = Literal["open", "add", "reduce", "close", "reverse", "hold", "reject"]


@dataclass(frozen=True)
class PositionIntent:
    decision_id: str
    strategy_id: str
    symbol: str
    inst_id: str
    action: PositionAction
    side: str
    size_usdt: float
    reduce_only: bool
    reason: str
    timestamp: datetime
    current_size_contracts: float = 0.0
    target_size_contracts: float | None = None
    metadata: Mapping[str, object] | None = None
