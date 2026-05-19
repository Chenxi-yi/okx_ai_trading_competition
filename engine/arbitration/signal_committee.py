"""Unified signal generation and portfolio arbitration helpers.

This module keeps strategy research signals separate from execution. Strategies
emit point-in-time candidates; the committee turns them into accepted paper/live
decisions under shared position and risk limits.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd

from arbitration.portfolio_arbiter import PortfolioArbiter, ArbitrationResult
from arbitration.thesis_exit import thesis_contract
from contracts import CandidateTrade, PortfolioState, Signal


TREND_PULLBACK_CANDIDATE_RULES: dict[str, dict[str, float | int]] = {
    "trend_pullback_reversal_cluster_elite_quality60_v1": {
        "priority": 300,
        "max_positions": 3,
        "budget_usdt": 50.0,
        "order_margin_usdt": 10.0,
    },
    "trend_pullback_reversal_quality_top20_v1": {
        "priority": 200,
        "max_positions": 3,
        "budget_usdt": 50.0,
        "order_margin_usdt": 10.0,
    },
    "trend_pullback_reversal_rank_top1_v1": {
        "priority": 100,
        "max_positions": 3,
        "budget_usdt": 50.0,
        "order_margin_usdt": 10.0,
    },
}


def build_committee_signals(
    candidates: pd.DataFrame,
    now_ts: pd.Timestamp,
    *,
    base_capital: float,
    base_risk: float,
    fee_slip_rate: float,
) -> list[Signal]:
    """Build C-Auto and derivatives-structure signals from one timestamp slice."""

    signals: list[Signal] = []
    for _, row in candidates.iterrows():
        c_auto = _c_auto_signal(row, now_ts, base_capital, base_risk, fee_slip_rate)
        if c_auto:
            signals.append(c_auto)
        trend_pullback = _trend_pullback_reversal_signal(row, now_ts, base_capital, base_risk, fee_slip_rate)
        if trend_pullback:
            signals.append(trend_pullback)
        daily_fib = _daily_fib_support_rebound_signal(row, now_ts, base_capital, base_risk, fee_slip_rate)
        if daily_fib:
            signals.append(daily_fib)
        signals.extend(_oi_compression_signals(row, now_ts, base_capital, base_risk, fee_slip_rate))
        signals.extend(_crowding_reversal_signals(row, now_ts, base_capital, base_risk, fee_slip_rate))
    return signals


def build_candidate_trades(
    candidates: pd.DataFrame,
    now_ts: pd.Timestamp,
    *,
    base_capital: float,
    base_risk: float,
    fee_slip_rate: float,
) -> list[CandidateTrade]:
    """Normalize strategy output into lifecycle candidate contracts."""

    return [candidate_trade_from_signal(signal) for signal in build_committee_signals(
        candidates,
        now_ts,
        base_capital=base_capital,
        base_risk=base_risk,
        fee_slip_rate=fee_slip_rate,
    )]


def candidate_trade_from_signal(signal: Signal) -> CandidateTrade:
    metadata = dict(signal.metadata)
    expected_edge = max(0.0, float(signal.p_target) - 0.5)
    feature_refs = tuple(str(item) for item in metadata.get("feature_refs", ()) if item)
    return CandidateTrade(
        strategy_id=signal.strategy_id,
        symbol=signal.symbol,
        side=signal.side,
        timestamp=signal.timestamp,
        entry_reference=float(signal.entry),
        horizon_sec=int(signal.horizon_sec),
        confidence=float(signal.confidence),
        expected_edge_pct=expected_edge,
        adverse_pct_estimate=float(signal.adverse_pct_estimate),
        target_reference=float(signal.target) if signal.target is not None else None,
        stop_reference=float(signal.stop) if signal.stop is not None else None,
        feature_refs=feature_refs,
        candidate_id=str(metadata.get("candidate_id") or ""),
        metadata=metadata,
    )


def candidate_trade_to_dict(candidate: CandidateTrade) -> dict[str, Any]:
    return {
        "candidate_id": candidate.candidate_id,
        "strategy_id": candidate.strategy_id,
        "symbol": candidate.symbol,
        "side": candidate.side,
        "timestamp": candidate.timestamp.isoformat(),
        "entry_reference": candidate.entry_reference,
        "horizon_sec": candidate.horizon_sec,
        "confidence": candidate.confidence,
        "expected_edge_pct": candidate.expected_edge_pct,
        "adverse_pct_estimate": candidate.adverse_pct_estimate,
        "target_reference": candidate.target_reference,
        "stop_reference": candidate.stop_reference,
        "feature_refs": list(candidate.feature_refs),
        "status": candidate.status,
        "metadata": dict(candidate.metadata),
    }


def arbitrate_signals(
    signals: list[Signal],
    positions: dict[str, dict[str, Any]],
    now_ts: pd.Timestamp,
    *,
    initial_capital: float,
    realized_nav: float,
    max_positions: int,
    max_decisions: int,
    max_total_budget_usdt: float,
    min_ev: float = 0.0,
    round_trip_cost_rate: float = 0.0,
) -> ArbitrationResult:
    """Run the shared committee for the current paper/live book."""

    portfolio = PortfolioState(
        timestamp=_to_datetime(now_ts),
        nav_usdt=float(realized_nav),
        free_usdt=max(0.0, float(initial_capital) - _used_budget(positions)),
        positions={},
        per_strategy_used=_per_strategy_used(positions),
        metadata={
            "open_symbols": sorted(positions),
            "per_strategy_open_count": _per_strategy_open_count(positions),
        },
    )
    arbiter = PortfolioArbiter(
        max_fractional_kelly=0.08,
        default_budget_usdt=25.0,
        min_ev=float(min_ev),
        max_decisions=int(max_decisions),
        max_positions=int(max_positions),
        max_total_budget_usdt=float(max_total_budget_usdt),
        strategy_priority={sid: int(rule["priority"]) for sid, rule in TREND_PULLBACK_CANDIDATE_RULES.items()},
        strategy_max_positions={sid: int(rule["max_positions"]) for sid, rule in TREND_PULLBACK_CANDIDATE_RULES.items()},
        strategy_budget_usdt={sid: float(rule["budget_usdt"]) for sid, rule in TREND_PULLBACK_CANDIDATE_RULES.items()},
        strategy_order_usdt={sid: float(rule["order_margin_usdt"]) for sid, rule in TREND_PULLBACK_CANDIDATE_RULES.items()},
        round_trip_cost_rate=float(round_trip_cost_rate),
    )
    return arbiter.arbitrate(signals, portfolio, now=_to_datetime(now_ts))


def _c_auto_signal(
    row: pd.Series,
    now_ts: pd.Timestamp,
    base_capital: float,
    base_risk: float,
    fee_slip_rate: float,
) -> Signal | None:
    if not bool(row.get("eligible", False)):
        return None
    side = str(row.get("side") or "")
    if side not in {"long", "short"}:
        return None
    entry = _float(row.get("close"))
    score = _float(row.get("score"), 0.0)
    risk_scalar = max(0.0, _float(row.get("risk_scalar"), 0.0))
    horizon_hours = max(1, int(_float(row.get("horizon_hours"), 6.0)))
    if entry <= 0 or risk_scalar <= 0:
        return None
    target_pct = max(0.035, abs(score))
    stop_pct = 0.025
    p_target = _clip(0.54 + min(abs(score), 0.02) * 3.0 - fee_slip_rate, 0.50, 0.64)
    return _signal(
        strategy_id="c_auto_v2_cross_section",
        symbol=str(row.get("symbol")),
        side=side,
        now_ts=now_ts,
        entry=entry,
        target_pct=target_pct,
        stop_pct=stop_pct,
        horizon_hours=horizon_hours,
        p_target=p_target,
        confidence=_clip(0.50 + abs(score) * 20.0, 0.50, 0.86),
        risk_budget_usdt=float(base_capital) * float(base_risk) * risk_scalar,
        metadata={
            "signal_family": row.get("signal_family"),
            "regime": row.get("btc_regime_6"),
            "score": score,
            "risk_scalar": risk_scalar,
            "leverage": 1.0,
            "size_semantics": "notional_usdt",
        },
    )


def _trend_pullback_reversal_signal(
    row: pd.Series,
    now_ts: pd.Timestamp,
    base_capital: float,
    base_risk: float,
    fee_slip_rate: float,
) -> Signal | None:
    if not bool(row.get("trend_pullback_eligible", False)):
        return None
    side = str(row.get("trend_pullback_side") or "")
    if side != "long":
        return None
    entry = _float(row.get("close"))
    score = _float(row.get("trend_pullback_score"), 0.0)
    if entry <= 0 or score <= 0:
        return None
    p_target = _clip(0.53 + min(score, 0.03) * 2.0 - fee_slip_rate, 0.51, 0.62)
    return _signal(
        strategy_id="trend_pullback_reversal_long",
        symbol=str(row.get("symbol")),
        side="long",
        now_ts=now_ts,
        entry=entry,
        target_pct=0.030,
        stop_pct=0.015,
        horizon_hours=6,
        p_target=p_target,
        confidence=_clip(0.52 + score * 12.0, 0.52, 0.78),
        risk_budget_usdt=float(base_capital) * float(base_risk) * 0.35,
        metadata={
            "signal_family": "trend_pullback_reversal",
            "regime": row.get("btc_regime_6"),
            "score": score,
            "h4_ret_6": _float(row.get("h4_ret_6")),
            "h4_ret_1": _float(row.get("h4_ret_1")),
            "ret_1": _float(row.get("ret_1")),
            "ret_3": _float(row.get("ret_3")),
            "close_to_high": _float(row.get("close_to_high")),
            "risk_scalar": 0.35,
            "leverage": 1.0,
            "size_semantics": "notional_usdt",
        },
    )


def _per_strategy_open_count(positions: dict[str, dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for pos in positions.values():
        sid = str(pos.get("source_strategy_id") or pos.get("strategy_id") or "")
        if sid:
            out[sid] = out.get(sid, 0) + 1
    return out


def _per_strategy_used(positions: dict[str, dict[str, Any]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for pos in positions.values():
        sid = str(pos.get("source_strategy_id") or pos.get("strategy_id") or "")
        if not sid:
            continue
        try:
            used = float(pos.get("margin_required_usdt") or pos.get("margin_usdt") or pos.get("risk_budget") or 0.0)
        except Exception:
            used = 0.0
        out[sid] = out.get(sid, 0.0) + max(0.0, used)
    return out


def _daily_fib_support_rebound_signal(
    row: pd.Series,
    now_ts: pd.Timestamp,
    base_capital: float,
    base_risk: float,
    fee_slip_rate: float,
) -> Signal | None:
    if not bool(row.get("daily_fib_eligible", False)):
        return None
    side = str(row.get("daily_fib_side") or "")
    if side != "long":
        return None
    entry = _float(row.get("close"))
    support = _float(row.get("daily_fib_support"))
    score = _float(row.get("daily_fib_score"), 0.0)
    if entry <= 0 or support <= 0 or score <= 0:
        return None
    stop_pct = max(0.012, min(0.028, entry / (support * (1.0 - 0.006)) - 1.0))
    target_pct = max(0.018, min(0.045, stop_pct * 1.8))
    p_target = _clip(0.54 + min(score, 0.03) * 2.5 - fee_slip_rate, 0.52, 0.64)
    return _signal(
        strategy_id="daily_fib_support_rebound_long",
        symbol=str(row.get("symbol")),
        side="long",
        now_ts=now_ts,
        entry=entry,
        target_pct=target_pct,
        stop_pct=stop_pct,
        horizon_hours=48,
        p_target=p_target,
        confidence=_clip(0.53 + score * 10.0, 0.53, 0.80),
        risk_budget_usdt=float(base_capital) * float(base_risk) * 0.30,
        metadata={
            "signal_family": "daily_fib_support_rebound",
            "structure_timeframe": "1d",
            "entry_timeframe": "4h",
            "fib_ratio": 0.786,
            "support": support,
            "score": score,
            "risk_scalar": 0.30,
            "leverage": 1.0,
            "size_semantics": "notional_usdt",
        },
    )


def _oi_compression_signals(
    row: pd.Series,
    now_ts: pd.Timestamp,
    base_capital: float,
    base_risk: float,
    fee_slip_rate: float,
) -> list[Signal]:
    entry = _float(row.get("close"))
    if entry <= 0:
        return []
    oi_z = _float(row.get("oi_z_24"))
    oi_chg_1h = _float(row.get("oi_chg_1h"))
    oi_chg_24h = _float(row.get("oi_chg_24h"))
    range_pct = abs(_float(row.get("range_pct")))
    rv_12 = abs(_float(row.get("rv_12")))
    ret_1 = _float(row.get("ret_1"))
    ret_3 = _float(row.get("ret_3"))
    ret_6 = _float(row.get("ret_6"))
    funding_z = _float(row.get("funding_z_24"))
    ls_z = _float(row.get("ls_z_24"))

    compression = (oi_z >= 1.0 or oi_chg_24h >= 0.05) and oi_chg_1h >= 0.005 and range_pct <= 0.035 and rv_12 <= 0.06
    if not compression:
        return []
    momentum = 0.5 * ret_1 + 0.3 * ret_3 + 0.2 * ret_6
    if abs(momentum) < 0.001:
        side = "short" if funding_z > 1.2 or ls_z > 1.0 else "long"
    else:
        side = "long" if momentum > 0 else "short"
    if side == "short" and bool(row.get("short_entries_disabled", False)):
        return []
    strength = _clip(0.50 + abs(momentum) * 10.0 + min(max(oi_z, 0.0), 4.0) * 0.025, 0.52, 0.68)
    return [
        _signal(
            strategy_id="deriv_oi_compression_breakout",
            symbol=str(row.get("symbol")),
            side=side,
            now_ts=now_ts,
            entry=entry,
            target_pct=0.028,
            stop_pct=0.018,
            horizon_hours=4,
            p_target=max(0.50, strength - fee_slip_rate),
            confidence=strength,
            risk_budget_usdt=float(base_capital) * float(base_risk) * 0.45,
            metadata={
                "oi_z_24": oi_z,
                "oi_chg_1h": oi_chg_1h,
                "oi_chg_24h": oi_chg_24h,
                "range_pct": range_pct,
                "rv_12": rv_12,
                "momentum": momentum,
            },
        )
    ]


def _crowding_reversal_signals(
    row: pd.Series,
    now_ts: pd.Timestamp,
    base_capital: float,
    base_risk: float,
    fee_slip_rate: float,
) -> list[Signal]:
    entry = _float(row.get("close"))
    if entry <= 0:
        return []
    oi_z = _float(row.get("oi_z_24"))
    funding_z = _float(row.get("funding_z_24"))
    ls_z = _float(row.get("ls_z_24"))
    close_to_high = _float(row.get("close_to_high"), 0.5)
    close_to_low = _float(row.get("close_to_low"), 0.5)
    ret_3 = _float(row.get("ret_3"))
    ret_6 = _float(row.get("ret_6"))

    crowded_long = oi_z >= 1.4 and funding_z >= 1.6 and ls_z >= 1.2 and (ret_3 < 0 or close_to_high < 0.45)
    crowded_short = oi_z >= 1.4 and funding_z <= -1.6 and ls_z <= -1.2 and (ret_3 > 0 or close_to_low < 0.45)
    if not crowded_long and not crowded_short:
        return []
    side = "short" if crowded_long else "long"
    if side == "short" and bool(row.get("short_entries_disabled", False)):
        return []
    crowd_score = min(4.0, max(0.0, oi_z) + abs(funding_z) * 0.5 + abs(ls_z) * 0.5)
    p_target = _clip(0.53 + crowd_score * 0.025 + abs(ret_6) * 2.0 - fee_slip_rate, 0.52, 0.70)
    return [
        _signal(
            strategy_id="deriv_crowding_reversal",
            symbol=str(row.get("symbol")),
            side=side,
            now_ts=now_ts,
            entry=entry,
            target_pct=0.035,
            stop_pct=0.020,
            horizon_hours=6,
            p_target=p_target,
            confidence=p_target,
            risk_budget_usdt=float(base_capital) * float(base_risk) * 0.50,
            metadata={
                "oi_z_24": oi_z,
                "funding_z_24": funding_z,
                "ls_z_24": ls_z,
                "close_to_high": close_to_high,
                "close_to_low": close_to_low,
                "crowding_side": "crowded_long" if crowded_long else "crowded_short",
            },
        )
    ]


def _signal(
    *,
    strategy_id: str,
    symbol: str,
    side: str,
    now_ts: pd.Timestamp,
    entry: float,
    target_pct: float,
    stop_pct: float,
    horizon_hours: int,
    p_target: float,
    confidence: float,
    risk_budget_usdt: float,
    metadata: dict[str, Any],
) -> Signal:
    if side == "long":
        target = entry * (1.0 + target_pct)
        stop = entry * (1.0 - stop_pct)
    else:
        target = entry * (1.0 - target_pct)
        stop = entry * (1.0 + stop_pct)
    enriched = dict(metadata)
    signal_family = str(enriched.get("signal_family") or strategy_id)
    regime = str(enriched.get("regime") or "")
    score = _float(enriched.get("score"), confidence)
    enriched["risk_budget_usdt"] = float(risk_budget_usdt)
    enriched["target_pct"] = float(target_pct)
    enriched["stop_pct"] = float(stop_pct)
    enriched["thesis_contract"] = thesis_contract(
        strategy_id=strategy_id,
        side=side,
        signal_family=signal_family,
        regime=regime,
        score=score,
    )
    return Signal(
        strategy_id=strategy_id,
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        timestamp=_to_datetime(now_ts),
        entry=float(entry),
        target=float(target),
        stop=float(stop),
        horizon_sec=int(horizon_hours * 3600),
        p_target=float(p_target),
        adverse_pct_estimate=float(stop_pct),
        confidence=float(confidence),
        metadata=enriched,
    )


def _used_budget(positions: dict[str, dict[str, Any]]) -> float:
    return sum(max(0.0, _float(pos.get("risk_budget"))) for pos in positions.values())


def _to_datetime(ts: pd.Timestamp | datetime) -> datetime:
    if isinstance(ts, datetime):
        return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts.astimezone(timezone.utc)
    if ts.tzinfo is None:
        return ts.tz_localize(timezone.utc).to_pydatetime()
    return ts.to_pydatetime()


def _float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except Exception:
        return default
    if pd.isna(out):
        return default
    return out


def _clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))
