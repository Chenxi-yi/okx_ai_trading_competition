"""Investment committee leverage and exposure policy helpers.

The committee may choose higher leverage later, but leverage is never a raw
multiplier. It is gated by account loss, stop distance, crowding, and external
alpha disagreement. The current C-Auto paper run still passes max_leverage=1,
so this module preserves today's behavior while making the weapon library
explicit and auditable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CommitteeLeverageInputs:
    requested_notional_usdt: float
    nav_usdt: float
    stop_pct: float
    requested_leverage: float = 1.0
    configured_max_leverage: float = 1.0
    max_position_nav_loss_pct: float = 0.0015
    max_stop_margin_loss_pct: float = 0.15
    same_side_open_count: int = 0
    same_symbol_open: bool = False
    kit_disagreement: bool = False
    kit_confirmation: bool = False
    stale_data_events: int = 0
    daily_loss_pct: float = 0.0
    max_daily_loss_pct: float = 0.015
    allow_aggressive_leverage: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


def compute_committee_leverage_policy(inputs: CommitteeLeverageInputs) -> dict[str, Any]:
    """Return an auditable leverage/notional decision for an accepted signal."""

    requested_notional = max(0.0, float(inputs.requested_notional_usdt))
    nav = max(0.0, float(inputs.nav_usdt))
    stop_pct = max(abs(float(inputs.stop_pct)), 0.001)
    requested_leverage = max(1.0, float(inputs.requested_leverage))
    configured_max = max(1.0, float(inputs.configured_max_leverage))
    nav_loss_cap_pct = max(0.0, float(inputs.max_position_nav_loss_pct))
    margin_loss_cap = max(0.001, float(inputs.max_stop_margin_loss_pct))

    risk_flags: list[str] = []
    rules: list[dict[str, Any]] = []
    notional_scalar = 1.0

    def add_rule(rule_id: str, action: str, value: float | bool | str, reason: str) -> None:
        rules.append({"rule_id": rule_id, "action": action, "value": value, "reason": reason})

    if inputs.stale_data_events > 0:
        risk_flags.append("stale_data")
        notional_scalar = min(notional_scalar, 0.0)
        add_rule("stale_data_veto", "notional_scalar_cap", 0.0, "stale data blocks new risk")

    if inputs.same_symbol_open:
        risk_flags.append("same_symbol_open")
        notional_scalar = min(notional_scalar, 0.0)
        add_rule("same_symbol_duplicate_veto", "notional_scalar_cap", 0.0, "do not add duplicate symbol exposure")

    if inputs.daily_loss_pct <= -abs(float(inputs.max_daily_loss_pct)):
        risk_flags.append("daily_loss_limit")
        notional_scalar = min(notional_scalar, 0.0)
        add_rule("daily_loss_veto", "notional_scalar_cap", 0.0, "daily loss limit reached")

    if inputs.same_side_open_count >= 3:
        risk_flags.append("same_side_concentration")
        notional_scalar = min(notional_scalar, 0.50)
        add_rule("same_side_concentration_scalar", "notional_scalar_cap", 0.50, "three or more same-side positions")
    elif inputs.same_side_open_count == 2:
        risk_flags.append("same_side_caution")
        notional_scalar = min(notional_scalar, 0.75)
        add_rule("same_side_caution_scalar", "notional_scalar_cap", 0.75, "two same-side positions already open")

    if inputs.kit_disagreement:
        risk_flags.append("kit_disagreement")
        notional_scalar = min(notional_scalar, 0.50)
        add_rule("kit_disagreement_scalar", "notional_scalar_cap", 0.50, "smartmoney/news/OI disagrees with signal")

    leverage_cap_by_margin_stop = max(1.0, margin_loss_cap / stop_pct)
    add_rule(
        "stop_margin_loss_cap",
        "leverage_cap",
        leverage_cap_by_margin_stop,
        "cap leverage so stop loss does not exceed margin-loss budget",
    )

    aggressive_cap = 5.0 if inputs.allow_aggressive_leverage and inputs.kit_confirmation and not risk_flags else 2.0
    add_rule(
        "aggressive_leverage_gate",
        "leverage_cap",
        aggressive_cap,
        "5x is only available with explicit aggressive mode, kit confirmation, and no risk flags",
    )

    leverage = min(requested_leverage, configured_max, leverage_cap_by_margin_stop, aggressive_cap)
    nav_loss_cap_notional = nav * nav_loss_cap_pct / stop_pct if nav_loss_cap_pct > 0 else requested_notional
    add_rule(
        "single_position_nav_loss_cap",
        "notional_cap",
        nav_loss_cap_notional,
        "cap notional so stop loss stays within account NAV budget",
    )

    notional = max(0.0, min(requested_notional * notional_scalar, nav_loss_cap_notional))
    margin_required = notional / leverage if leverage > 0 else notional
    stop_account_loss_usdt = notional * stop_pct
    blocked = notional <= 0.0

    return {
        "policy_id": "committee_leverage_weapon_library_v1",
        "size_semantics": "notional_usdt",
        "requested_notional_usdt": float(requested_notional),
        "notional_usdt": float(notional),
        "notional_scalar": float(notional_scalar),
        "requested_leverage": float(requested_leverage),
        "configured_max_leverage": float(configured_max),
        "leverage": float(leverage),
        "margin_required_usdt": float(margin_required),
        "stop_pct": float(stop_pct),
        "max_position_nav_loss_pct": float(nav_loss_cap_pct),
        "max_stop_margin_loss_pct": float(margin_loss_cap),
        "nav_loss_cap_notional_usdt": float(nav_loss_cap_notional),
        "stop_account_loss_usdt": float(stop_account_loss_usdt),
        "stop_account_loss_pct": float(stop_account_loss_usdt / nav) if nav > 0 else 0.0,
        "stop_margin_loss_pct": float(stop_pct * leverage),
        "risk_flags": risk_flags,
        "rules": rules,
        "blocked": bool(blocked),
        "metadata": dict(inputs.metadata),
    }


def infer_kit_alignment(metadata: dict[str, Any]) -> tuple[bool, bool]:
    """Return (disagreement, confirmation) from optional Kit alpha metadata."""

    alignment = str(metadata.get("kit_signal_alignment") or metadata.get("external_alpha_alignment") or "").lower()
    if alignment in {"disagree", "against", "veto"}:
        return True, False
    if alignment in {"agree", "confirm", "confirmed"}:
        return False, True
    if metadata.get("kit_disagreement") is True:
        return True, False
    if metadata.get("kit_confirmation") is True:
        return False, True
    return False, False
