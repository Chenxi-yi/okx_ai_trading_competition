"""Shared thesis-monitoring contract for committee-approved positions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from contracts import Signal


THESIS_CONTRACT_VERSION = "investment_committee_thesis_exit_v1"


@dataclass(frozen=True)
class ThesisExitDecision:
    action: str
    reason: str
    severity: str
    matched_signal: Signal | None = None
    current_score: float | None = None
    entry_score: float | None = None
    details: dict[str, Any] | None = None

    @property
    def should_exit(self) -> bool:
        return self.action == "exit"


def thesis_contract(
    *,
    strategy_id: str,
    side: str,
    signal_family: str,
    regime: str,
    score: float,
) -> dict[str, Any]:
    """Contract attached to every committee-approved order."""

    return {
        "contract_id": THESIS_CONTRACT_VERSION,
        "granted_at": datetime.now(timezone.utc).isoformat(),
        "entry_strategy_id": strategy_id,
        "entry_side": side,
        "entry_signal_family": signal_family or strategy_id,
        "entry_regime": regime,
        "entry_score": float(score),
        "monitor": [
            "same strategy still emits a same-side signal for the symbol",
            "regime and signal family remain compatible with the entry thesis",
            "current score remains above the configured retention floor",
            "hard invalidation exits immediately; score decay reduces exposure, or exits full-size for micro positions",
        ],
        "decay_action": "reduce_or_exit_micro",
        "invalidation_action": "exit",
    }


def evaluate_position_thesis(
    position: dict[str, Any],
    current_signals: Iterable[Signal],
    *,
    score_retain: float = 0.60,
    min_score: float = 0.0001,
) -> ThesisExitDecision:
    """Decide whether an open position still satisfies its entry thesis."""

    symbol = str(position.get("symbol") or "")
    strategy_id = str(position.get("source_strategy_id") or position.get("strategy_id") or "")
    side = str(position.get("side") or "")
    entry_family = str(position.get("signal_family") or strategy_id)
    entry_regime = str(position.get("regime") or "")
    entry_score = _float(position.get("score"))

    symbol_signals = [sig for sig in current_signals if str(sig.symbol) == symbol]
    same_strategy = [sig for sig in symbol_signals if str(sig.strategy_id) == strategy_id]
    if not same_strategy:
        return ThesisExitDecision(
            action="exit",
            reason="thesis_signal_absent",
            severity="invalidated",
            entry_score=entry_score,
            details={"strategy_id": strategy_id, "symbol_signals": [sig.strategy_id for sig in symbol_signals]},
        )

    same_side = [sig for sig in same_strategy if str(sig.side) == side]
    if not same_side:
        return ThesisExitDecision(
            action="exit",
            reason="thesis_side_flip",
            severity="invalidated",
            entry_score=entry_score,
            details={"current_sides": sorted({str(sig.side) for sig in same_strategy})},
        )

    best = max(same_side, key=lambda sig: float(sig.confidence or 0.0))
    current_family = str(best.metadata.get("signal_family") or best.strategy_id)
    current_regime = str(best.metadata.get("regime") or "")
    current_score = _float(best.metadata.get("score"), default=float(best.confidence or 0.0))

    if entry_family and current_family and current_family != entry_family:
        return ThesisExitDecision(
            action="exit",
            reason="thesis_family_change",
            severity="invalidated",
            matched_signal=best,
            current_score=current_score,
            entry_score=entry_score,
            details={"entry_family": entry_family, "current_family": current_family},
        )
    if entry_regime and current_regime and current_regime != entry_regime:
        return ThesisExitDecision(
            action="exit",
            reason="thesis_regime_change",
            severity="invalidated",
            matched_signal=best,
            current_score=current_score,
            entry_score=entry_score,
            details={"entry_regime": entry_regime, "current_regime": current_regime},
        )

    retain_floor = max(float(min_score), abs(float(entry_score or 0.0)) * float(score_retain))
    if current_score is None or current_score < retain_floor:
        return ThesisExitDecision(
            action="exit",
            reason="thesis_score_decay",
            severity="decayed",
            matched_signal=best,
            current_score=current_score,
            entry_score=entry_score,
            details={"retain_floor": retain_floor},
        )

    return ThesisExitDecision(
        action="hold",
        reason="thesis_intact",
        severity="ok",
        matched_signal=best,
        current_score=current_score,
        entry_score=entry_score,
    )


def _float(value: Any, default: float | None = None) -> float | None:
    try:
        out = float(value)
    except Exception:
        return default
    return out
