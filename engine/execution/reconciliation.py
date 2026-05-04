"""Live account reconciliation skeleton."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from contracts import PortfolioState


@dataclass(frozen=True)
class ReconciliationResult:
    ok: bool
    reason: str
    local_positions: int
    external_positions: int
    reduce_only_required: bool = False


class AccountReconciler:
    """Compare local accounting state against an external account snapshot."""

    def reconcile(
        self,
        portfolio: PortfolioState,
        external_positions: Mapping[str, float],
        tolerance_contracts: float = 1e-9,
    ) -> ReconciliationResult:
        local = {
            position.inst_id: position.size_contracts if position.side == "long" else -position.size_contracts
            for position in portfolio.positions.values()
        }
        symbols = set(local) | set(external_positions)
        mismatches = []
        for symbol in symbols:
            if abs(float(local.get(symbol, 0.0)) - float(external_positions.get(symbol, 0.0))) > tolerance_contracts:
                mismatches.append(symbol)
        if mismatches:
            return ReconciliationResult(
                ok=False,
                reason="position mismatch: " + ",".join(sorted(mismatches)),
                local_positions=len(local),
                external_positions=len(external_positions),
                reduce_only_required=True,
            )
        return ReconciliationResult(True, "ok", len(local), len(external_positions), False)
