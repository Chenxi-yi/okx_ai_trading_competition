"""Account-level pre-trade risk gate."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping

from contracts import Decision, MarketState, PortfolioState


@dataclass(frozen=True)
class RiskDecision:
    decision: Decision
    approved: bool
    reason: str
    scalar: float = 1.0


@dataclass
class AccountRiskConfig:
    max_gross_leverage: float = 1.5
    max_symbol_exposure_pct: float = 0.25
    max_strategy_exposure_pct: float = 0.30
    max_daily_loss_pct: float = 0.05
    max_drawdown_pct: float = 0.20
    max_data_age_sec: float = 180.0
    min_free_usdt: float = 5.0
    speculative_book_scalar: float = 0.10
    tactical_book_scalar: float = 0.50
    core_book_scalar: float = 1.0


@dataclass
class AccountRiskArbiter:
    """Final pre-trade authority before order construction."""

    config: AccountRiskConfig = field(default_factory=AccountRiskConfig)

    def evaluate(
        self,
        decisions: list[Decision] | tuple[Decision, ...],
        portfolio: PortfolioState,
        market: MarketState,
        strategy_books: Mapping[str, str] | None = None,
    ) -> tuple[RiskDecision, ...]:
        strategy_books = strategy_books or {}
        out: list[RiskDecision] = []

        stale = not market.is_fresh(self.config.max_data_age_sec) if market.freshness_sec else False
        if stale:
            return tuple(
                RiskDecision(d, False, "market data stale", 0.0)
                for d in decisions
            )

        if portfolio.free_usdt < self.config.min_free_usdt:
            return tuple(
                RiskDecision(d, False, "insufficient free USDT", 0.0)
                for d in decisions
            )

        risk_state = dict(portfolio.risk_state or {})
        daily_loss_pct = float(risk_state.get("daily_loss_pct", 0.0) or 0.0)
        drawdown_pct = float(risk_state.get("drawdown_pct", 0.0) or 0.0)
        if daily_loss_pct >= self.config.max_daily_loss_pct:
            return tuple(RiskDecision(d, False, "daily loss limit reached", 0.0) for d in decisions)
        if drawdown_pct >= self.config.max_drawdown_pct:
            return tuple(RiskDecision(d, False, "drawdown limit reached", 0.0) for d in decisions)

        for decision in decisions:
            scalar = self._book_scalar(strategy_books.get(decision.signal.strategy_id, "core"))
            capped_size = min(decision.size_usdt * scalar, self._symbol_cap(portfolio))
            if capped_size <= 0:
                out.append(RiskDecision(decision, False, "risk cap reduced size to zero", 0.0))
                continue
            if capped_size < decision.size_usdt:
                adjusted = Decision(
                    signal=decision.signal,
                    size_usdt=capped_size,
                    reason=f"{decision.reason}; account risk scalar={scalar:.2f}",
                    timestamp=datetime.now(timezone.utc),
                    decision_id=decision.decision_id,
                    rejected=decision.rejected,
                    arbiter_id=decision.arbiter_id,
                    risk_scalar=scalar,
                    metadata={**dict(decision.metadata), "risk_adjusted": True},
                )
                out.append(RiskDecision(adjusted, True, "approved with size adjustment", scalar))
            else:
                out.append(RiskDecision(decision, True, "approved", scalar))
        return tuple(out)

    def _book_scalar(self, book: str) -> float:
        if book == "speculative":
            return self.config.speculative_book_scalar
        if book == "tactical":
            return self.config.tactical_book_scalar
        return self.config.core_book_scalar

    def _symbol_cap(self, portfolio: PortfolioState) -> float:
        return max(0.0, portfolio.nav_usdt * self.config.max_symbol_exposure_pct)
