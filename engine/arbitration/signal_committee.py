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
from contracts import PortfolioState, Signal


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
        signals.extend(_oi_compression_signals(row, now_ts, base_capital, base_risk, fee_slip_rate))
        signals.extend(_crowding_reversal_signals(row, now_ts, base_capital, base_risk, fee_slip_rate))
    return signals


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
) -> ArbitrationResult:
    """Run the shared committee for the current paper/live book."""

    portfolio = PortfolioState(
        timestamp=_to_datetime(now_ts),
        nav_usdt=float(realized_nav),
        free_usdt=max(0.0, float(initial_capital) - _used_budget(positions)),
        positions={},
        metadata={"open_symbols": sorted(positions)},
    )
    arbiter = PortfolioArbiter(
        max_fractional_kelly=0.08,
        default_budget_usdt=25.0,
        min_ev=float(min_ev),
        max_decisions=int(max_decisions),
        max_positions=int(max_positions),
        max_total_budget_usdt=float(max_total_budget_usdt),
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
    enriched["risk_budget_usdt"] = float(risk_budget_usdt)
    enriched["target_pct"] = float(target_pct)
    enriched["stop_pct"] = float(stop_pct)
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


def _to_datetime(ts: pd.Timestamp) -> datetime:
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
