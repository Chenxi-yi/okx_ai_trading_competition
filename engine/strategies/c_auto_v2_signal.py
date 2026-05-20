"""Signal-only adapter for C-Auto v2 cross-section candidates.

This module deliberately has no OKX gateway, ownership journal, or account I/O.
It converts point-in-time scored features into canonical committee signals and
accepted decisions. Runtime adapters may execute the returned decisions through
the environment pipeline, but strategy code must stay side-effect free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from arbitration.signal_committee import (
    arbitrate_signals,
    build_committee_signals,
    candidate_trade_from_signal,
    candidate_trade_to_dict,
)
from contracts import Decision, Signal


@dataclass(frozen=True)
class CAutoV2SignalConfig:
    min_volume_usd: float
    min_score_quantile: float
    per_symbol_margin_usdt: float
    daily_budget_usdt: float
    post_exit_cooldown_hours: float
    short_loss_cooldown_hours: float
    short_loss_lookback_hours: float
    short_loss_cooldown_min_losses: int
    fee_bps_per_side: float = 5.0
    slippage_bps_per_side: float = 2.0
    require_slow_confirm: bool = False
    signal_base_risk: float = 0.06


@dataclass(frozen=True)
class CAutoV2SignalResult:
    signals: tuple[Signal, ...]
    decisions: tuple[Decision, ...]
    candidate_contracts: tuple[dict[str, Any], ...]
    events: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    notes: tuple[str, ...] = field(default_factory=tuple)


def generate_c_auto_v2_signal_decisions(
    scored: pd.DataFrame,
    *,
    now_ts: pd.Timestamp,
    positions: dict[str, dict[str, Any]],
    max_positions: int,
    max_decisions: int,
    used_margin_usdt: float,
    cooldown_symbols: set[str] | None,
    risk_events: list[dict[str, Any]] | None,
    config: CAutoV2SignalConfig,
) -> CAutoV2SignalResult:
    """Return signal/committee decisions without executing or journaling orders."""

    slots = max(0, int(max_positions) - len(positions))
    slots = min(slots, max(0, int(max_decisions)))
    if slots <= 0:
        return CAutoV2SignalResult(
            signals=(),
            decisions=(),
            candidate_contracts=(),
            events=(_event(now_ts, "skip", None, None, "micro_live_no_slot"),),
        )

    group = _timestamp_group(scored, now_ts)
    group = group[~group["symbol"].isin(positions)].copy()
    events: list[dict[str, Any]] = []

    blocked_by_cooldown = set(cooldown_symbols or set())
    if blocked_by_cooldown:
        group = group[~group["symbol"].astype(str).isin(blocked_by_cooldown)]
        for symbol in sorted(blocked_by_cooldown):
            events.append(
                _event(
                    now_ts,
                    "committee_note",
                    symbol,
                    None,
                    f"rejected post_exit_cooldown_{float(config.post_exit_cooldown_hours):g}h",
                )
            )

    group["volume_usd"] = pd.to_numeric(group["volume_usd"], errors="coerce").fillna(0.0)
    group = group[group["volume_usd"] >= float(config.min_volume_usd)].copy()
    if group.empty:
        return CAutoV2SignalResult(signals=(), decisions=(), candidate_contracts=(), events=tuple(events))

    if bool(config.require_slow_confirm):
        slow_ok = _slow_confirm_ok(group)
        if bool((~slow_ok).any()):
            group.loc[:, "slow_confirm_ok"] = slow_ok
            group.loc[~slow_ok, "blocked_by_slow_confirm"] = True
            group.loc[~slow_ok, "eligible"] = False

    short_cooldown = _short_loss_cooldown_status(
        risk_events or [],
        now_ts,
        cooldown_hours=float(config.short_loss_cooldown_hours),
        lookback_hours=float(config.short_loss_lookback_hours),
        min_losses=int(config.short_loss_cooldown_min_losses),
    )
    if short_cooldown["active"]:
        short_mask = group["side"].astype(str) == "short"
        if bool(short_mask.any()):
            group.loc[short_mask, "eligible"] = False
            group.loc[short_mask, "short_entries_disabled"] = True
            events.append(
                {
                    **_event(
                        now_ts,
                        "committee_note",
                        None,
                        "short",
                        f"rejected portfolio_short_loss_cooldown_{float(config.short_loss_cooldown_hours):g}h",
                    ),
                    "short_cooldown": short_cooldown,
                }
            )

    eligible = group[group["eligible"].astype(bool)].copy()
    if not eligible.empty:
        threshold = eligible.groupby("side")["score"].transform(lambda s: s.quantile(float(config.min_score_quantile)))
        selected_symbols = set(eligible[eligible["score"] >= threshold]["symbol"].astype(str))
        group.loc[~group["symbol"].astype(str).isin(selected_symbols), "eligible"] = False

    signal_base_capital = max(
        float(config.per_symbol_margin_usdt) / float(config.signal_base_risk),
        float(config.per_symbol_margin_usdt),
    )
    signals = tuple(
        build_committee_signals(
            group,
            now_ts,
            base_capital=signal_base_capital,
            base_risk=float(config.signal_base_risk),
            fee_slip_rate=_round_trip_cost_rate(config),
        )
    )
    candidate_contracts = tuple(candidate_trade_to_dict(candidate_trade_from_signal(signal)) for signal in signals)
    if candidate_contracts:
        events.append(
            {
                **_event(now_ts, "candidate_contracts", None, None, "normalized_candidate_trade_contracts"),
                "candidate_count": len(candidate_contracts),
                "candidates": list(candidate_contracts[:25]),
            }
        )

    result = arbitrate_signals(
        signals,
        positions,
        now_ts,
        initial_capital=float(config.daily_budget_usdt),
        realized_nav=float(config.daily_budget_usdt),
        max_positions=max_positions,
        max_decisions=slots,
        max_total_budget_usdt=max(0.0, float(config.daily_budget_usdt) - float(used_margin_usdt)),
        min_ev=0.0,
        round_trip_cost_rate=_round_trip_cost_rate(config),
    )
    return CAutoV2SignalResult(
        signals=signals,
        decisions=tuple(result.decisions[:slots]),
        candidate_contracts=candidate_contracts,
        events=tuple(events),
        notes=tuple(result.notes),
    )


def _round_trip_cost_rate(config: CAutoV2SignalConfig) -> float:
    return 2.0 * (float(config.fee_bps_per_side) + float(config.slippage_bps_per_side)) / 10_000.0


def _timestamp_group(scored: pd.DataFrame, now_ts: pd.Timestamp) -> pd.DataFrame:
    sliced = scored.xs(now_ts, level="timestamp", drop_level=False)
    try:
        return sliced.reset_index()
    except ValueError:
        return sliced.reset_index(drop=True)


def _slow_confirm_ok(group: pd.DataFrame) -> pd.Series:
    side = group["side"].astype(str)
    ret_1 = pd.to_numeric(group.get("ret_1", 0.0), errors="coerce").fillna(0.0)
    h4_ret_1 = pd.to_numeric(group.get("h4_ret_1", 0.0), errors="coerce").fillna(0.0)
    h4_ret_6 = pd.to_numeric(group.get("h4_ret_6", 0.0), errors="coerce").fillna(0.0)
    oi_z = pd.to_numeric(group.get("oi_z_24", 0.0), errors="coerce").fillna(0.0)
    ls_z = pd.to_numeric(group.get("ls_z_24", 0.0), errors="coerce").fillna(0.0)
    funding_z = pd.to_numeric(group.get("funding_z_24", 0.0), errors="coerce").fillna(0.0)
    short_ok = (h4_ret_6 < -0.006) | (h4_ret_1 < -0.002) | (((oi_z > 0.35) | (funding_z > 0.25) | (ls_z > 0.35)) & (ret_1 < 0))
    long_ok = (h4_ret_6 > 0.006) | (h4_ret_1 > 0.002) | (((oi_z > 0.35) | (funding_z < -0.25) | (ls_z < -0.35)) & (ret_1 > 0))
    return ((side == "short") & short_ok) | ((side == "long") & long_ok)


def _short_loss_cooldown_status(
    risk_events: list[dict[str, Any]],
    now_ts: pd.Timestamp,
    *,
    cooldown_hours: float,
    lookback_hours: float,
    min_losses: int,
) -> dict[str, Any]:
    if min_losses <= 0 or cooldown_hours <= 0 or lookback_hours <= 0:
        return {"active": False, "losses": 0}
    cutoff = now_ts - pd.Timedelta(hours=float(lookback_hours))
    losses = []
    for event in risk_events:
        if str(event.get("side") or "") != "short":
            continue
        if str(event.get("event") or "") not in {"exit", "thesis_exit", "stop_exit", "risk_exit"}:
            continue
        try:
            ts = pd.Timestamp(event.get("ts"))
        except Exception:
            continue
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        ts = ts.tz_convert(now_ts.tz) if now_ts.tzinfo else ts.tz_localize(None)
        try:
            pnl = float(event.get("pnl") or event.get("realized_pnl") or 0.0)
        except Exception:
            pnl = 0.0
        if ts >= cutoff and pnl < 0:
            losses.append({"ts": ts.isoformat(), "pnl": pnl, "symbol": event.get("symbol")})
    active = len(losses) >= int(min_losses)
    return {
        "active": active,
        "losses": len(losses),
        "cooldown_until": (now_ts + pd.Timedelta(hours=float(cooldown_hours))).isoformat() if active else None,
        "recent_losses": losses[-10:],
    }


def _event(ts: pd.Timestamp, event: str, symbol: str | None, side: str | None, reason: str) -> dict[str, Any]:
    return {
        "ts": ts.isoformat(),
        "event": event,
        "symbol": symbol,
        "side": side,
        "reason": reason,
    }
