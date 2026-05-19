"""Portfolio-level signal arbitration."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

from contracts import Decision, PortfolioState, Signal


@dataclass(frozen=True)
class ArbitrationResult:
    decisions: tuple[Decision, ...]
    rejected: tuple[Signal, ...]
    notes: tuple[str, ...] = ()


@dataclass
class PortfolioArbiter:
    """Selects the best signals before account-level risk and execution."""

    arbiter_id: str = "kelly_ev_arbiter_v1"
    max_fractional_kelly: float = 0.25
    default_budget_usdt: float = 25.0
    min_ev: float = 0.0
    max_decisions: int = 4
    max_positions: int = 4
    max_total_budget_usdt: float | None = None
    strategy_priority: dict[str, int] = field(default_factory=dict)
    strategy_max_positions: dict[str, int] = field(default_factory=dict)
    strategy_budget_usdt: dict[str, float] = field(default_factory=dict)
    strategy_order_usdt: dict[str, float] = field(default_factory=dict)
    round_trip_cost_rate: float = 0.0

    def arbitrate(
        self,
        signals: Iterable[Signal],
        portfolio: PortfolioState,
        now: datetime | None = None,
    ) -> ArbitrationResult:
        now = now or datetime.now(timezone.utc)
        grouped: dict[str, list[Signal]] = defaultdict(list)
        for signal in signals:
            grouped[signal.symbol].append(signal)

        decisions: list[Decision] = []
        rejected: list[Signal] = []
        notes: list[str] = []
        open_symbols = {str(item) for item in (portfolio.metadata.get("open_symbols") or [])}
        per_strategy_open = {
            str(k): int(v)
            for k, v in dict(portfolio.metadata.get("per_strategy_open_count") or {}).items()
        }
        per_strategy_used = {str(k): float(v) for k, v in dict(portfolio.per_strategy_used or {}).items()}

        winners: list[tuple[Signal, tuple[Signal, ...]]] = []
        for symbol, candidates in sorted(grouped.items()):
            ranked = sorted(candidates, key=self._symbol_rank_key, reverse=True)
            winner = ranked[0]
            losers = tuple(ranked[1:])
            rejected.extend(losers)
            winners.append((winner, losers))

        used_budget = 0.0
        open_slots = max(0, int(self.max_positions) - int(portfolio.gross_position_count))
        decision_slots = min(max(0, int(self.max_decisions)), open_slots)
        new_strategy_count: dict[str, int] = defaultdict(int)
        new_strategy_budget: dict[str, float] = defaultdict(float)
        for winner, losers in sorted(winners, key=lambda item: self._rank_key(item[0]), reverse=True):
            if winner.symbol in open_symbols:
                rejected.append(winner)
                notes.append(f"{winner.symbol}: rejected same-symbol duplicate exposure")
                continue
            if len(decisions) >= decision_slots:
                rejected.append(winner)
                notes.append(f"{winner.symbol}: rejected no portfolio slot")
                continue
            ev = winner.forward_ev
            net_ev = None if ev is None else ev - max(0.0, float(self.round_trip_cost_rate))
            if net_ev is not None and net_ev < self.min_ev:
                rejected.append(winner)
                notes.append(
                    f"{winner.symbol}: rejected net EV {net_ev:.6f} "
                    f"after cost {float(self.round_trip_cost_rate):.6f}"
                )
                continue

            size = self._size_usdt(winner, portfolio)
            strategy_id = str(winner.strategy_id)
            strategy_order_cap = self.strategy_order_usdt.get(strategy_id)
            if strategy_order_cap is not None:
                size = min(size, max(0.0, float(strategy_order_cap)))
            strategy_max = self.strategy_max_positions.get(strategy_id)
            if strategy_max is not None and per_strategy_open.get(strategy_id, 0) + new_strategy_count[strategy_id] >= int(strategy_max):
                rejected.append(winner)
                notes.append(f"{winner.symbol}: rejected {strategy_id} max positions {int(strategy_max)}")
                continue
            strategy_budget = self.strategy_budget_usdt.get(strategy_id)
            if strategy_budget is not None:
                remaining_strategy = max(
                    0.0,
                    float(strategy_budget) - per_strategy_used.get(strategy_id, 0.0) - new_strategy_budget[strategy_id],
                )
                size = min(size, remaining_strategy)
            if self.max_total_budget_usdt is not None:
                remaining = max(0.0, float(self.max_total_budget_usdt) - used_budget)
                size = min(size, remaining)
            if size <= 0:
                rejected.append(winner)
                notes.append(f"{winner.symbol}: rejected zero size")
                continue

            used_budget += size
            new_strategy_count[strategy_id] += 1
            new_strategy_budget[strategy_id] += size
            open_symbols.add(winner.symbol)
            reason = self._reason(winner, losers)
            decisions.append(
                Decision(
                    signal=winner,
                    size_usdt=size,
                    reason=reason,
                    rejected=losers,
                    arbiter_id=self.arbiter_id,
                    timestamp=now,
                    metadata={
                        "forward_ev": ev,
                        "net_forward_ev": net_ev,
                        "round_trip_cost_rate": float(self.round_trip_cost_rate),
                        "kelly_fraction": winner.kelly_fraction,
                    },
                )
            )

        return ArbitrationResult(
            decisions=tuple(decisions),
            rejected=tuple(rejected),
            notes=tuple(notes),
        )

    def _rank_key(self, signal: Signal) -> tuple[float, float, float]:
        ev = signal.forward_ev
        kelly = signal.kelly_fraction
        return (
            ev if ev is not None else -1e9,
            kelly if kelly is not None else 0.0,
            signal.confidence,
        )

    def _symbol_rank_key(self, signal: Signal) -> tuple[float, float, float, float]:
        ev = signal.forward_ev
        kelly = signal.kelly_fraction
        priority = float(self.strategy_priority.get(str(signal.strategy_id), 0))
        return (
            priority,
            ev if ev is not None else -1e9,
            kelly if kelly is not None else 0.0,
            signal.confidence,
        )

    def _size_usdt(self, signal: Signal, portfolio: PortfolioState) -> float:
        requested = signal.metadata.get("risk_budget_usdt") if signal.metadata else None
        if requested is not None:
            try:
                return max(0.0, min(float(requested), max(portfolio.free_usdt, 0.0)))
            except Exception:
                pass
        kelly = signal.kelly_fraction
        if kelly is None:
            return min(self.default_budget_usdt, max(portfolio.free_usdt, 0.0))
        fraction = min(kelly, self.max_fractional_kelly)
        return max(0.0, min(portfolio.free_usdt, portfolio.nav_usdt * fraction))

    @staticmethod
    def _reason(winner: Signal, losers: tuple[Signal, ...]) -> str:
        ev = winner.forward_ev
        parts = [
            f"accepted {winner.strategy_id} {winner.side}",
            f"confidence={winner.confidence:.3f}",
        ]
        if ev is not None:
            parts.append(f"ev={ev:.6f}")
        if losers:
            parts.append(f"rejected={len(losers)} competing signals")
        return "; ".join(parts)
