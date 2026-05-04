"""Decision journal event contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from .orders import Fill, OrderIntent
from .portfolio import PortfolioState
from .signals import Decision, Signal


@dataclass(frozen=True)
class JournalOutcome:
    mfe_pct: float | None = None
    mae_pct: float | None = None
    forward_return_pct: float | None = None
    realized_pnl_usdt: float | None = None
    notes: str = ""


@dataclass(frozen=True)
class DecisionEvent:
    timestamp: datetime
    environment: str
    strategy_id: str
    decision_id: str
    market_state_ref: str | None = None
    feature_version: str | None = None
    signal: Signal | None = None
    decision: Decision | None = None
    order_intent: OrderIntent | None = None
    fill: Fill | None = None
    portfolio: PortfolioState | None = None
    outcome: JournalOutcome | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RiskEvent:
    timestamp: datetime
    severity: str
    code: str
    message: str
    action: str
    decision_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
