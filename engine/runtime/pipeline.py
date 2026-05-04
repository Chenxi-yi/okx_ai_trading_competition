"""Canonical Signal -> Decision -> Risk -> Order -> Fill pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

from arbitration import PortfolioArbiter
from contracts import (
    DecisionEvent,
    Fill,
    InstrumentSpec,
    MarketState,
    OrderIntent,
    PortfolioState,
    Strategy,
    StrategyContext,
)
from execution.router import ExecutionConfig, ExecutionRouter
from observability import DecisionJournal
from position import PositionIntent, PositionManager
from risk import (
    AccountRiskArbiter,
    InstrumentRiskArbiter,
    KillSwitch,
    RiskDecision,
    StrategyRiskArbiter,
)


@dataclass
class PipelineConfig:
    environment: str = "paper"
    feature_version: str | None = None
    journal_dir: Path | str = Path("engine/logs/decision_journal")
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)


@dataclass(frozen=True)
class PipelineResult:
    signals_count: int
    decisions_count: int
    position_intents: tuple[PositionIntent, ...]
    position_held: tuple[PositionIntent, ...]
    position_rejected: tuple[PositionIntent, ...]
    approved_count: int
    orders: tuple[OrderIntent, ...]
    fills: tuple[Fill, ...]
    risk_decisions: tuple[RiskDecision, ...]


class TradingPipeline:
    """Runs one strategy cycle through the canonical architecture."""

    def __init__(
        self,
        strategies: Sequence[Strategy],
        arbiter: PortfolioArbiter,
        account_risk: AccountRiskArbiter,
        execution_router: ExecutionRouter,
        position_manager: PositionManager | None = None,
        strategy_risk: StrategyRiskArbiter | None = None,
        instrument_risk: InstrumentRiskArbiter | None = None,
        kill_switch: KillSwitch | None = None,
        config: PipelineConfig | None = None,
        journal: DecisionJournal | None = None,
    ):
        self.strategies = list(strategies)
        self.arbiter = arbiter
        self.position_manager = position_manager or PositionManager()
        self.account_risk = account_risk
        self.strategy_risk = strategy_risk or StrategyRiskArbiter()
        self.instrument_risk = instrument_risk or InstrumentRiskArbiter()
        self.kill_switch = kill_switch or KillSwitch()
        self.execution_router = execution_router
        self.config = config or PipelineConfig()
        self.journal = journal or DecisionJournal(self.config.journal_dir)

    def run_once(
        self,
        market: MarketState,
        portfolio: PortfolioState,
        mark_prices: Mapping[str, float],
        execution_prices: Mapping[str, float] | None = None,
    ) -> PipelineResult:
        execution_prices = execution_prices or mark_prices
        signals = []
        strategy_books: dict[str, str] = {}
        kill_state = self.kill_switch.state()
        if kill_state.active:
            return PipelineResult(
                signals_count=0,
                decisions_count=0,
                position_intents=(),
                position_held=(),
                position_rejected=(),
                approved_count=0,
                orders=(),
                fills=(),
                risk_decisions=(),
            )
        for strategy in self.strategies:
            strategy_books[strategy.strategy_id] = strategy.spec.book
            ctx = StrategyContext(market=market, portfolio=portfolio, config={})
            signals.extend(strategy.generate(ctx))

        arbitration = self.arbiter.arbitrate(signals, portfolio)
        exit_decisions = self.position_manager.exit_decisions(
            portfolio=portfolio,
            market=market,
            mark_prices=mark_prices,
        )
        position_plan = self.position_manager.plan(
            (*exit_decisions, *arbitration.decisions),
            portfolio=portfolio,
            market=market,
            mark_prices=mark_prices,
        )
        strategy_filtered = []
        rejected_by_strategy: list[RiskDecision] = []
        for decision in position_plan.decisions:
            strategy_decision = self.strategy_risk.evaluate(decision, portfolio)
            if strategy_decision.approved:
                strategy_filtered.append(decision)
            else:
                rejected_by_strategy.append(RiskDecision(decision, False, strategy_decision.reason, 0.0))

        risk_decisions = tuple(rejected_by_strategy) + self.account_risk.evaluate(
            tuple(strategy_filtered),
            portfolio=portfolio,
            market=market,
            strategy_books=strategy_books,
        )

        orders: list[OrderIntent] = []
        fills: list[Fill] = []
        for risk_decision in risk_decisions:
            decision = risk_decision.decision
            if not risk_decision.approved:
                self._journal_decision(market, portfolio, decision=decision, fill=None, order=None)
                continue
            instrument = self._instrument_for(decision.signal.symbol, market)
            if instrument is None:
                self._journal_decision(market, portfolio, decision=decision, fill=None, order=None)
                continue
            order = self.execution_router.build_order(decision, instrument, self.config.execution)
            instrument_decision = self.instrument_risk.evaluate_order(decision, instrument, order)
            if not instrument_decision.approved:
                self._journal_decision(market, portfolio, decision=decision, fill=None, order=order)
                continue
            if order.size_contracts <= 0:
                self._journal_decision(market, portfolio, decision=decision, fill=None, order=order)
                continue
            price = float(execution_prices.get(decision.signal.symbol) or mark_prices.get(decision.signal.symbol) or decision.signal.entry)
            fill = self.execution_router.execute(order, price)
            orders.append(order)
            fills.append(fill)
            self._journal_decision(market, portfolio, decision=decision, fill=fill, order=order)

        return PipelineResult(
            signals_count=len(signals),
            decisions_count=len(arbitration.decisions),
            position_intents=position_plan.intents,
            position_held=position_plan.held,
            position_rejected=position_plan.rejected,
            approved_count=sum(1 for rd in risk_decisions if rd.approved),
            orders=tuple(orders),
            fills=tuple(fills),
            risk_decisions=tuple(risk_decisions),
        )

    def _instrument_for(self, symbol: str, market: MarketState) -> InstrumentSpec | None:
        direct = market.instruments.get(symbol)
        if direct:
            return direct
        inst_id = symbol if symbol.endswith("-USDT-SWAP") else symbol.replace("/", "-") + "-SWAP"
        return market.instruments.get(inst_id)

    def _journal_decision(
        self,
        market: MarketState,
        portfolio: PortfolioState,
        decision,
        order: OrderIntent | None,
        fill: Fill | None,
    ) -> None:
        self.journal.append_decision(
            DecisionEvent(
                timestamp=datetime.now(timezone.utc),
                environment=self.config.environment,
                strategy_id=decision.signal.strategy_id,
                decision_id=decision.decision_id,
                market_state_ref=str(market.timestamp),
                feature_version=self.config.feature_version,
                signal=decision.signal,
                decision=decision,
                order_intent=order,
                fill=fill,
                portfolio=portfolio,
            )
        )
