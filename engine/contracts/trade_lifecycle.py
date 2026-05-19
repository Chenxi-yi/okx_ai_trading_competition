"""Trade lifecycle contracts from candidate signal to reconciled execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Mapping


CandidateSide = Literal["long", "short"]
CandidateStatus = Literal["candidate", "approved", "rejected", "expired"]
ExecutionStatus = Literal["submitted", "filled", "partial", "rejected", "cancelled", "unknown"]


@dataclass(frozen=True)
class CandidateTrade:
    """Strategy adapter output before committee approval.

    Strategies may compute candidate entries, but they must not own account
    sizing or exchange-side execution. This contract is the handoff boundary.
    """

    strategy_id: str
    symbol: str
    side: CandidateSide
    timestamp: datetime
    entry_reference: float
    horizon_sec: int
    confidence: float
    expected_edge_pct: float
    adverse_pct_estimate: float
    target_reference: float | None = None
    stop_reference: float | None = None
    feature_refs: tuple[str, ...] = ()
    candidate_id: str = ""
    status: CandidateStatus = "candidate"
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ApprovedTradePlan:
    """Committee-approved instruction for the position manager."""

    decision_id: str
    candidate: CandidateTrade
    environment: str
    okx_profile: str
    margin_usdt: float
    notional_usdt: float
    leverage: float
    stop_price: float | None
    target_price: float | None
    max_account_loss_usdt: float
    approved_at: datetime
    committee_id: str = "investment_committee"
    risk_policy_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionReceipt:
    """Execution layer result after submitting an approved plan."""

    decision_id: str
    environment: str
    okx_profile: str
    inst_id: str
    status: ExecutionStatus
    submitted_at: datetime
    filled_at: datetime | None = None
    order_ids: Mapping[str, Any] = field(default_factory=dict)
    fill_price: float | None = None
    filled_contracts: float = 0.0
    fee_usdt: float = 0.0
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReconciliationSnapshot:
    """Exchange truth snapshot used to validate internal state."""

    environment: str
    okx_profile: str
    checked_at: datetime
    positions: Mapping[str, Any] = field(default_factory=dict)
    open_orders: Mapping[str, Any] = field(default_factory=dict)
    algo_orders: Mapping[str, Any] = field(default_factory=dict)
    fills: tuple[Mapping[str, Any], ...] = ()
    ok: bool = True
    errors: tuple[str, ...] = ()
