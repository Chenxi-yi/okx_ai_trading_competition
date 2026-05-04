"""Strategy-level risk state."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from contracts import Decision, PortfolioState


@dataclass(frozen=True)
class StrategyRiskDecision:
    approved: bool
    reason: str


@dataclass(frozen=True)
class StrategyRiskConfig:
    max_strategy_exposure_pct: float = 0.30
    paused_strategy_ids: tuple[str, ...] = ()


@dataclass
class StrategyRiskArbiter:
    config: StrategyRiskConfig = field(default_factory=StrategyRiskConfig)

    def evaluate(self, decision: Decision, portfolio: PortfolioState) -> StrategyRiskDecision:
        strategy_id = decision.signal.strategy_id
        if strategy_id in self.config.paused_strategy_ids:
            return StrategyRiskDecision(False, "strategy paused")
        used = float(portfolio.per_strategy_used.get(strategy_id, 0.0) or 0.0)
        cap = portfolio.nav_usdt * self.config.max_strategy_exposure_pct
        if used + decision.size_usdt > cap:
            return StrategyRiskDecision(False, "strategy exposure cap reached")
        return StrategyRiskDecision(True, "approved")
