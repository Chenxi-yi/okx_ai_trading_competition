"""Paper runner skeleton for the professional signal pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Callable, Mapping, Sequence

from accounting import AccountingConfig, PortfolioAccounting
from arbitration import PortfolioArbiter
from contracts import InstrumentSpec, MarketState, Strategy
from execution.router import ExecutionConfig, PaperExecutionRouter
from risk.account import AccountRiskArbiter
from runtime.pipeline import PipelineConfig, TradingPipeline


MarketSnapshotProvider = Callable[[], tuple[MarketState, Mapping[str, float]]]


@dataclass(frozen=True)
class PaperRunnerConfig:
    initial_nav_usdt: float = 10_000.0
    journal_dir: Path | str = Path("engine/logs/paper_journal")
    status_path: Path | str | None = Path("engine/logs/paper_status.json")
    stale_after_sec: float = 180.0
    execution: ExecutionConfig = field(default_factory=lambda: ExecutionConfig(profile="demo"))


class PaperRunner:
    """Runs one paper cycle at a time.

    A scheduler can call `run_once` every bar. Keeping this class single-cycle
    makes crash recovery and dashboard integration easier than embedding sleeps
    inside the runner itself.
    """

    def __init__(
        self,
        strategies: Sequence[Strategy],
        market_provider: MarketSnapshotProvider,
        instruments: Mapping[str, InstrumentSpec] | None = None,
        config: PaperRunnerConfig | None = None,
    ):
        self.config = config or PaperRunnerConfig()
        self.market_provider = market_provider
        self.instruments = dict(instruments or {})
        self.accounting = PortfolioAccounting(AccountingConfig(initial_nav_usdt=self.config.initial_nav_usdt))
        self.pipeline = TradingPipeline(
            strategies=strategies,
            arbiter=PortfolioArbiter(),
            account_risk=AccountRiskArbiter(),
            execution_router=PaperExecutionRouter(self.config.execution),
            config=PipelineConfig(
                environment="paper",
                journal_dir=self.config.journal_dir,
                execution=self.config.execution,
            ),
        )

    def run_once(self) -> dict:
        market, mark_prices = self.market_provider()
        if self.instruments and not market.instruments:
            market = MarketState(
                timestamp=market.timestamp,
                universe=market.universe,
                ohlcv=market.ohlcv,
                orderbooks=market.orderbooks,
                funding=market.funding,
                open_interest=market.open_interest,
                long_short_ratio=market.long_short_ratio,
                instruments=self.instruments,
                features=market.features,
                freshness_sec=market.freshness_sec,
                metadata=market.metadata,
            )
        portfolio = self.accounting.state(_as_utc(market.timestamp), mark_prices)
        result = self.pipeline.run_once(market, portfolio, mark_prices)
        orders_by_decision = {order.decision_id: order for order in result.orders}
        for fill in result.fills:
            order = orders_by_decision.get(fill.decision_id)
            if order:
                self.accounting.apply_fill(fill, order, _as_utc(market.timestamp))
        state = self.accounting.state(_as_utc(market.timestamp), mark_prices)
        heartbeat_at = datetime.now(timezone.utc)
        market_ts = _as_utc(market.timestamp)
        status_age_sec = max(0.0, (heartbeat_at - market_ts).total_seconds())
        status = {
            "runner_status": "ok",
            "heartbeat_at": heartbeat_at.isoformat(),
            "timestamp": state.timestamp.isoformat(),
            "market_age_sec": status_age_sec,
            "stale": status_age_sec > self.config.stale_after_sec,
            "signals": result.signals_count,
            "decisions": result.decisions_count,
            "position_intents": len(result.position_intents),
            "position_held": len(result.position_held),
            "position_rejected": len(result.position_rejected),
            "approved": result.approved_count,
            "orders": len(result.orders),
            "fills": len(result.fills),
            "nav_usdt": state.nav_usdt,
            "free_usdt": state.free_usdt,
            "positions": state.gross_position_count,
            "total_fees_usdt": state.total_fees_usdt,
            "total_funding_usdt": state.metadata.get("total_funding_usdt", 0.0),
        }
        self._write_status(status)
        return status

    def _write_status(self, status: Mapping[str, object]) -> None:
        if self.config.status_path is None:
            return
        path = Path(self.config.status_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(status, indent=2, sort_keys=True))


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
