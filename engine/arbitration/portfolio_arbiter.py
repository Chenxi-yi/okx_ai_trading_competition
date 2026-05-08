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

        winners: list[tuple[Signal, tuple[Signal, ...]]] = []
        for symbol, candidates in sorted(grouped.items()):
            ranked = sorted(candidates, key=self._rank_key, reverse=True)
            winner = ranked[0]
            losers = tuple(ranked[1:])
            rejected.extend(losers)
            winners.append((winner, losers))

        used_budget = 0.0
        open_slots = max(0, int(self.max_positions) - int(portfolio.gross_position_count))
        decision_slots = min(max(0, int(self.max_decisions)), open_slots)
        for winner, losers in sorted(winners, key=lambda item: self._rank_key(item[0]), reverse=True):
            if len(decisions) >= decision_slots:
                rejected.append(winner)
                notes.append(f"{winner.symbol}: rejected no portfolio slot")
                continue
            ev = winner.forward_ev
            if ev is not None and ev < self.min_ev:
                rejected.append(winner)
                notes.append(f"{winner.symbol}: rejected negative EV {ev:.6f}")
                continue

            size = self._size_usdt(winner, portfolio)
            if self.max_total_budget_usdt is not None:
                remaining = max(0.0, float(self.max_total_budget_usdt) - used_budget)
                size = min(size, remaining)
            if size <= 0:
                rejected.append(winner)
                notes.append(f"{winner.symbol}: rejected zero size")
                continue

            used_budget += size
            reason = self._reason(winner, losers)
            decisions.append(
                Decision(
                    signal=winner,
                    size_usdt=size,
                    reason=reason,
                    rejected=losers,
                    arbiter_id=self.arbiter_id,
                    timestamp=now,
                    metadata={"forward_ev": ev, "kelly_fraction": winner.kelly_fraction},
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
