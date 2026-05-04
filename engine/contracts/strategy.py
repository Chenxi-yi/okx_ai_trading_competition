"""Strategy Protocol and strategy specification contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Protocol

from .market import MarketState
from .portfolio import PortfolioState
from .signals import RegimeLabel, Signal

Book = Literal["core", "tactical", "speculative"]


@dataclass(frozen=True)
class StrategySpec:
    strategy_id: str
    hypothesis: str
    book: Book
    timeframe: str
    holding_period: str
    symbols_or_universe: str
    required_data: tuple[str, ...]
    required_features: tuple[str, ...]
    allowed_regimes: tuple[str, ...]
    entry_logic: str
    exit_logic: str
    position_sizing: str
    risk_budget: str
    expected_failure_modes: tuple[str, ...]
    backtest_window: str
    paper_requirement: str
    live_enable_default: bool = False
    owner_notes: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StrategyContext:
    market: MarketState
    portfolio: PortfolioState
    regime: RegimeLabel | None = None
    config: Mapping[str, Any] = field(default_factory=dict)


class Strategy(Protocol):
    strategy_id: str
    spec: StrategySpec

    def generate(self, context: StrategyContext) -> list[Signal]:
        """Generate point-in-time signals. Strategies must not place orders."""
        ...
