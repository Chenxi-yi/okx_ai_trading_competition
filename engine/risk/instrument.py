"""Instrument-level pre-trade risk checks."""

from __future__ import annotations

from dataclasses import dataclass

from contracts import Decision, InstrumentSpec, OrderIntent


@dataclass(frozen=True)
class InstrumentRiskDecision:
    approved: bool
    reason: str


@dataclass(frozen=True)
class InstrumentRiskConfig:
    max_spread_bps: float = 20.0
    require_active: bool = True


class InstrumentRiskArbiter:
    def __init__(self, config: InstrumentRiskConfig | None = None):
        self.config = config or InstrumentRiskConfig()

    def evaluate_order(self, decision: Decision, instrument: InstrumentSpec, order: OrderIntent) -> InstrumentRiskDecision:
        if self.config.require_active and not instrument.active:
            return InstrumentRiskDecision(False, "instrument inactive")
        if order.size_contracts < instrument.min_sz:
            return InstrumentRiskDecision(False, "order below min size")
        if instrument.max_mkt_sz is not None and order.size_contracts > instrument.max_mkt_sz:
            return InstrumentRiskDecision(False, "order exceeds max market size")
        return InstrumentRiskDecision(True, "approved")
