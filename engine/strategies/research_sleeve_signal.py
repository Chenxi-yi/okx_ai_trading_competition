"""Signal-only helpers for lightweight research sleeves.

The research sleeve runners still own scheduling and state serialization, but
candidate normalization and committee submission live here so the strategy layer
has a side-effect-free signal contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pandas as pd

from arbitration.signal_committee import (
    arbitrate_signals,
    candidate_trade_from_signal,
    candidate_trade_to_dict,
)
from arbitration.thesis_exit import thesis_contract
from contracts import Decision, Signal


@dataclass(frozen=True)
class ResearchSleeveSignalConfig:
    strategy_id: str
    initial_capital: float
    max_positions: int
    round_trip_cost_rate: float


@dataclass(frozen=True)
class ResearchSleeveSignalResult:
    signals: tuple[Signal, ...]
    decisions: tuple[Decision, ...]
    accepted_candidates: tuple[dict[str, Any], ...]
    candidate_contracts: tuple[dict[str, Any], ...]
    event: dict[str, Any]


def submit_research_candidates_to_committee(
    candidates: list[dict[str, Any]],
    *,
    positions: dict[str, Any],
    realized_nav: float,
    now: str,
    config: ResearchSleeveSignalConfig,
) -> ResearchSleeveSignalResult:
    """Convert candidate rows to signals and arbitrate without executing."""

    now_ts = pd.Timestamp(now)
    signals = tuple(candidate_to_signal(config.strategy_id, cand, now_ts, config.round_trip_cost_rate) for cand in candidates)
    by_key = {(signal.symbol, signal.side): cand for signal, cand in zip(signals, candidates)}
    candidate_contracts = tuple(candidate_trade_to_dict(candidate_trade_from_signal(signal)) for signal in signals)
    budget_used = sum(float(getattr(pos, "notional", 0.0)) / max(float(getattr(pos, "leverage", 1.0)), 1.0) for pos in positions.values())
    budget_total = max(0.0, float(config.initial_capital) - budget_used)
    result = arbitrate_signals(
        signals,
        {symbol: vars(pos) if hasattr(pos, "__dict__") else dict(pos) for symbol, pos in positions.items()},
        now_ts,
        initial_capital=float(config.initial_capital),
        realized_nav=float(realized_nav),
        max_positions=int(config.max_positions),
        max_decisions=int(config.max_positions),
        max_total_budget_usdt=budget_total,
        min_ev=0.0,
        round_trip_cost_rate=float(config.round_trip_cost_rate),
    )
    accepted: list[dict[str, Any]] = []
    for decision in result.decisions:
        cand = dict(by_key.get((decision.signal.symbol, decision.signal.side)) or {})
        if not cand:
            continue
        cand["committee_decision_id"] = decision.decision_id
        cand["committee_reason"] = decision.reason
        cand["committee_size_usdt"] = decision.size_usdt
        accepted.append(cand)
    event = {
        "ts": now,
        "event": "committee_submission",
        "strategy_id": config.strategy_id,
        "source_strategy_id": config.strategy_id,
        "candidate_count": len(candidates),
        "signal_count": len(signals),
        "candidate_contract_count": len(candidate_contracts),
        "candidate_contracts": list(candidate_contracts[:25]),
        "accepted_count": len(accepted),
        "rejected_count": len(result.rejected),
        "notes": list(result.notes),
        "accepted": [
            {
                "symbol": decision.signal.symbol,
                "side": decision.signal.side,
                "decision_id": decision.decision_id,
                "size_usdt": decision.size_usdt,
                "reason": decision.reason,
                "forward_ev": decision.signal.forward_ev,
            }
            for decision in result.decisions
        ],
        "rejected": [
            {
                "symbol": signal.symbol,
                "side": signal.side,
                "strategy_id": signal.strategy_id,
                "forward_ev": signal.forward_ev,
            }
            for signal in result.rejected
        ],
    }
    return ResearchSleeveSignalResult(
        signals=signals,
        decisions=tuple(result.decisions),
        accepted_candidates=tuple(accepted),
        candidate_contracts=candidate_contracts,
        event=event,
    )


def candidate_to_signal(strategy_id: str, cand: dict[str, Any], now_ts: pd.Timestamp, round_trip_cost_rate: float) -> Signal:
    entry = float(cand["entry_price"])
    side = str(cand["side"])
    target = cand.get("target")
    stop = cand.get("stop")
    target_pct = abs(float(target) / entry - 1.0) if target is not None and entry > 0 else 0.03
    stop_pct = abs(float(stop) / entry - 1.0) if stop is not None and entry > 0 else 0.015
    quality = float((cand.get("thesis_contract") or {}).get("quality_score") or 0.0)
    p_target = max(0.51, min(0.66, 0.53 + quality * 0.12 - float(round_trip_cost_rate)))
    confidence = max(0.52, min(0.82, 0.52 + quality * 0.25))
    metadata = dict(cand.get("thesis_contract") or {})
    metadata.update(
        {
            "signal_family": cand.get("signal_family") or strategy_id,
            "score": quality,
            "risk_budget_usdt": float(cand.get("budget") or 0.0),
            "target_pct": target_pct,
            "stop_pct": stop_pct,
            "thesis_contract": thesis_contract(
                strategy_id=strategy_id,
                side=side,
                signal_family=str(cand.get("signal_family") or strategy_id),
                regime=str(metadata.get("regime") or ""),
                score=quality,
            ),
        }
    )
    timestamp = now_ts.to_pydatetime()
    if not isinstance(timestamp, datetime):
        timestamp = datetime.utcnow()
    return Signal(
        strategy_id=strategy_id,
        symbol=str(cand["symbol"]),
        side=side,  # type: ignore[arg-type]
        timestamp=timestamp,
        entry=entry,
        target=float(target) if target is not None else None,
        stop=float(stop) if stop is not None else None,
        horizon_sec=int(float((cand.get("thesis_contract") or {}).get("max_hold_hours") or 12) * 3600),
        p_target=p_target,
        adverse_pct_estimate=stop_pct,
        confidence=confidence,
        metadata=metadata,
    )
