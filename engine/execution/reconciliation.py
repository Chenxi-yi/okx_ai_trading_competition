"""Live account reconciliation skeleton."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

from contracts import PortfolioState, ReconciliationSnapshot


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

    def reconcile_owned_live_positions(
        self,
        owned_positions: Mapping[str, Mapping[str, object]],
        exchange_positions: Mapping[str, Mapping[str, object] | float],
        *,
        environment: str,
        okx_profile: str,
        tolerance_contracts: float = 1e-9,
    ) -> ReconciliationSnapshot:
        """Compare journal-rebuilt ownership against exchange positions."""

        owned = {
            str(inst_id): abs(_position_size(row))
            for inst_id, row in owned_positions.items()
            if abs(_position_size(row)) > tolerance_contracts
        }
        exchange = {
            str(inst_id): abs(_position_size(row))
            for inst_id, row in exchange_positions.items()
            if abs(_position_size(row)) > tolerance_contracts
        }
        errors: list[str] = []
        for inst_id in sorted(set(owned) | set(exchange)):
            if abs(owned.get(inst_id, 0.0) - exchange.get(inst_id, 0.0)) > tolerance_contracts:
                errors.append(f"ownership_mismatch:{inst_id}")
        return ReconciliationSnapshot(
            environment=environment,
            okx_profile=okx_profile,
            checked_at=datetime.now(timezone.utc),
            positions={
                "owned": {key: dict(value) for key, value in owned_positions.items()},
                "exchange": {key: value for key, value in exchange_positions.items()},
            },
            ok=not errors,
            errors=tuple(errors),
        )


def _position_size(value: Mapping[str, object] | float) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    for key in ("filled_contracts", "contracts", "exchange_contracts", "size_contracts", "pos"):
        raw = value.get(key)
        try:
            return float(raw)
        except Exception:
            continue
    return 0.0
