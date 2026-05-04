"""Signal, regime, and decision contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Mapping
from uuid import uuid4

Side = Literal["long", "short"]


@dataclass(frozen=True)
class RegimeLabel:
    state: str
    confidence: float
    timestamp: datetime
    classifier_id: str
    classifier_version: str
    sub_features: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Signal:
    strategy_id: str
    symbol: str
    side: Side
    timestamp: datetime
    entry: float
    target: float | None
    stop: float | None
    horizon_sec: int
    p_target: float
    adverse_pct_estimate: float
    confidence: float
    regime: RegimeLabel | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def gain_pct(self) -> float | None:
        if self.target is None or self.entry <= 0:
            return None
        raw = self.target / self.entry - 1.0
        return raw if self.side == "long" else -raw

    @property
    def loss_pct(self) -> float:
        if self.stop is None or self.entry <= 0:
            return self.adverse_pct_estimate
        raw = self.stop / self.entry - 1.0
        return abs(raw)

    @property
    def forward_ev(self) -> float | None:
        gain = self.gain_pct
        if gain is None:
            return None
        return self.p_target * gain - (1.0 - self.p_target) * self.loss_pct

    @property
    def kelly_fraction(self) -> float | None:
        gain = self.gain_pct
        loss = self.loss_pct
        if gain is None or gain <= 0 or loss <= 0:
            return None
        b = gain / loss
        return max(0.0, min(1.0, (self.p_target * (b + 1.0) - 1.0) / b))


@dataclass(frozen=True)
class Decision:
    signal: Signal
    size_usdt: float
    reason: str
    timestamp: datetime
    decision_id: str = field(default_factory=lambda: str(uuid4()))
    rejected: tuple[Signal, ...] = ()
    arbiter_id: str = "portfolio_arbiter"
    risk_scalar: float = 1.0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def accepted(self) -> bool:
        return self.size_usdt > 0
