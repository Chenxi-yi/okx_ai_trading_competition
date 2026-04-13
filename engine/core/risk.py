"""
core/risk.py
============
Composable risk models for the trading algorithm pipeline.

Maps to LEAN's RiskManagementModel. Stack multiple RiskModel instances
in a CompositeRiskModel — each receives the weights output by the
previous stage. Short-circuits when all weights reach zero.

Phase 3 models (fine-grained, each independently configurable):
  DrawdownCircuitBreakerModel — peak/drawdown tracking → NORMAL/REDUCED/CASH
  VolRegimeModel              — rolling NAV vol → LOW/MEDIUM/HIGH size scalar
  CorrelationWatchdogModel    — avg pairwise corr → 0.6× if clustered

Compatibility shim (Phase 2 monolith, now delegates to the three above):
  EngineRiskModel             — single adapter, same behaviour as before

Usage:
    # Explicit composable stack (Phase 3+)
    risk = CompositeRiskModel([
        DrawdownCircuitBreakerModel(),
        VolRegimeModel(),
        CorrelationWatchdogModel(),
    ])

    # Profile-tuned (e.g. tighter CB for hourly)
    risk = CompositeRiskModel([
        DrawdownCircuitBreakerModel(threshold_reduced=0.08, threshold_cash=0.15),
        VolRegimeModel(),
        CorrelationWatchdogModel(threshold=0.80),
    ])
"""

from __future__ import annotations

import logging
import math
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from config.settings import (
    CORRELATION_THRESHOLD,
    DRAWDOWN_CIRCUIT_BREAKER_1,
    DRAWDOWN_CIRCUIT_BREAKER_2,
    HIGH_VOL_PERCENTILE,
    LOW_VOL_PERCENTILE,
)

logger = logging.getLogger(__name__)

# Scalars for each state / regime
_CB_SCALARS = {"NORMAL": 1.0, "REDUCED": 0.5, "CASH": 0.0}
_VOL_SCALARS = {"LOW": 1.25, "MEDIUM": 1.0, "HIGH": 0.5}
_CORR_SCALAR = 0.6   # applied when watchdog triggers
_MIN_NAV_HISTORY = 30  # bars needed before vol/corr models activate


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class RiskModel(ABC):
    """
    Adjust target weights according to a risk rule.

    Returns (adjusted_weights, risk_summary).
    adjusted_weights: same or scaled-down/zeroed weights
    risk_summary:     dict forwarded to the structured logger
    """

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def adjust(
        self,
        weights: Dict[str, float],
        portfolio,
        price_data: Dict[str, pd.DataFrame],
        prices: Dict[str, float],
        profile: Dict[str, Any],
    ) -> Tuple[Dict[str, float], Dict[str, Any]]: ...


# ---------------------------------------------------------------------------
# CompositeRiskModel
# ---------------------------------------------------------------------------

class CompositeRiskModel:
    """
    Chains RiskModel instances in sequence.

    Each model receives the weights output by the previous one.
    Short-circuits the chain if all weights become zero.
    """

    def __init__(self, models: List[RiskModel]):
        self.models = models

    def adjust(
        self,
        weights: Dict[str, float],
        portfolio,
        price_data: Dict[str, pd.DataFrame],
        prices: Dict[str, float],
        profile: Dict[str, Any],
    ) -> Tuple[Dict[str, float], Dict[str, Any]]:
        combined: Dict[str, Any] = {}
        for model in self.models:
            weights, summary = model.adjust(weights, portfolio, price_data, prices, profile)
            combined.update(summary)
            if not any(abs(w) > 1e-8 for w in weights.values()):
                logger.info(
                    "CompositeRiskModel: all weights zeroed after '%s' — skipping remaining models",
                    model.name,
                )
                break
        return weights, combined


# ---------------------------------------------------------------------------
# DrawdownCircuitBreakerModel
# ---------------------------------------------------------------------------

class DrawdownCircuitBreakerModel(RiskModel):
    """
    Monitors portfolio drawdown from peak NAV.

    States and their effect on position sizing:
      NORMAL   (dd < threshold_reduced)  →  1.0× (no change)
      REDUCED  (dd >= threshold_reduced) →  0.5× (halve all positions)
      CASH     (dd >= threshold_cash)    →  0.0× (zero all positions)

    Recovery rules (prevent being locked in CASH forever):
      - After cooldown_days days in CASH → reset to NORMAL, reset peak to current
      - NAV recovers to within recovery_pct of peak → reset to NORMAL

    State persisted in portfolio.risk_state:
      peak_nav, circuit_breaker_state, circuit_breaker_cash_since
    """

    def __init__(
        self,
        threshold_reduced: float = DRAWDOWN_CIRCUIT_BREAKER_1,
        threshold_cash: float = DRAWDOWN_CIRCUIT_BREAKER_2,
        recovery_pct: float = 0.05,
        cooldown_days: int = 90,
    ):
        self.threshold_reduced = threshold_reduced
        self.threshold_cash = threshold_cash
        self.recovery_pct = recovery_pct
        self.cooldown_days = cooldown_days

    @property
    def name(self) -> str:
        return "drawdown_circuit_breaker"

    def adjust(
        self,
        weights: Dict[str, float],
        portfolio,
        price_data: Dict[str, pd.DataFrame],
        prices: Dict[str, float],
        profile: Dict[str, Any],
    ) -> Tuple[Dict[str, float], Dict[str, Any]]:
        current_nav = portfolio.nav(prices)
        rs = portfolio.risk_state

        # Update peak NAV
        peak_nav = rs.get("peak_nav", portfolio.initial_capital)
        if current_nav > peak_nav:
            peak_nav = current_nav
        rs["peak_nav"] = peak_nav

        dd = (peak_nav - current_nav) / peak_nav if peak_nav > 0 else 0.0
        today = pd.Timestamp(datetime.now(timezone.utc).date())

        cb_state = rs.get("circuit_breaker_state", "NORMAL")
        cash_since_raw = rs.get("circuit_breaker_cash_since")
        cash_since = pd.Timestamp(cash_since_raw) if cash_since_raw else None

        # --- Recovery: time-based ---
        if cb_state == "CASH" and cash_since is not None:
            days_in_cash = (today - cash_since).days
            if days_in_cash >= self.cooldown_days:
                logger.info(
                    "Circuit breaker RESET (cooldown %dd) | peak $%.2f → $%.2f",
                    days_in_cash, peak_nav, current_nav,
                )
                cb_state = "NORMAL"
                cash_since = None
                peak_nav = current_nav   # reset peak
                rs["peak_nav"] = peak_nav

        # --- Recovery: price-based (within recovery_pct of peak) ---
        if dd <= self.recovery_pct and cb_state != "NORMAL":
            logger.info(
                "Circuit breaker RESET (recovery) | NAV=$%.2f | peak=$%.2f",
                current_nav, peak_nav,
            )
            cb_state = "NORMAL"
            cash_since = None

        # --- State transitions ---
        if dd >= self.threshold_cash:
            if cb_state != "CASH":
                logger.warning(
                    "CIRCUIT BREAKER → CASH | drawdown=%.1f%% ≥ %.0f%% | zeroing positions",
                    dd * 100, self.threshold_cash * 100,
                )
                cash_since = today
            cb_state = "CASH"

        elif dd >= self.threshold_reduced:
            if cb_state == "NORMAL":
                logger.warning(
                    "CIRCUIT BREAKER → REDUCED | drawdown=%.1f%% ≥ %.0f%% | halving positions",
                    dd * 100, self.threshold_reduced * 100,
                )
            cb_state = "REDUCED"
            cash_since = None

        else:
            if cb_state != "NORMAL":
                cb_state = "NORMAL"
            cash_since = None

        # Persist
        rs["circuit_breaker_state"] = cb_state
        rs["circuit_breaker_cash_since"] = str(cash_since) if cash_since else None

        scalar = _CB_SCALARS.get(cb_state, 1.0)
        adjusted = {sym: w * scalar for sym, w in weights.items()}

        return adjusted, {
            "circuit_breaker_state": cb_state,
            "drawdown_pct": round(dd * 100, 2),
            "peak_nav": round(peak_nav, 2),
            "cb_scalar": scalar,
        }


# ---------------------------------------------------------------------------
# VolRegimeModel
# ---------------------------------------------------------------------------

class VolRegimeModel(RiskModel):
    """
    Detects the current volatility regime from portfolio NAV history.

    Uses rolling annualised realised vol with hysteresis to avoid
    constant flipping on noisy data.

    Scalars applied to all weights:
      LOW    → 1.25× (vol is below normal — allow modest upsize)
      MEDIUM → 1.0×  (no change)
      HIGH   → 0.5×  (vol is elevated — halve all positions)

    State persisted in portfolio.risk_state: vol_regime
    """

    def __init__(
        self,
        window: int = 30,
        high_pct: int = HIGH_VOL_PERCENTILE,
        low_pct: int = LOW_VOL_PERCENTILE,
        hysteresis: float = 0.05,
    ):
        self.window = window
        self.high_pct = high_pct
        self.low_pct = low_pct
        self.hysteresis = hysteresis

    @property
    def name(self) -> str:
        return "vol_regime"

    def adjust(
        self,
        weights: Dict[str, float],
        portfolio,
        price_data: Dict[str, pd.DataFrame],
        prices: Dict[str, float],
        profile: Dict[str, Any],
    ) -> Tuple[Dict[str, float], Dict[str, Any]]:
        rs = portfolio.risk_state
        current_regime = rs.get("vol_regime", "MEDIUM")

        nav_entries = portfolio.nav_history
        if len(nav_entries) < _MIN_NAV_HISTORY:
            # Not enough history — stay MEDIUM
            rs["vol_regime"] = "MEDIUM"
            return dict(weights), {"vol_regime": "MEDIUM", "vol_scalar": 1.0}

        nav_series = pd.Series(
            [e["nav"] for e in nav_entries],
            index=pd.DatetimeIndex([e["date"] for e in nav_entries]),
        )

        # Annualisation factor from profile
        periods_per_year = profile.get("periods_per_year", 365)
        new_regime = self._detect(nav_series, current_regime, periods_per_year)

        if new_regime != current_regime:
            logger.info(
                "Vol regime change: %s → %s", current_regime, new_regime,
            )
        rs["vol_regime"] = new_regime

        scalar = _VOL_SCALARS.get(new_regime, 1.0)
        adjusted = {sym: w * scalar for sym, w in weights.items()} if scalar != 1.0 else dict(weights)

        return adjusted, {"vol_regime": new_regime, "vol_scalar": scalar}

    def _detect(self, nav_series: pd.Series, current: str, periods_per_year: int) -> str:
        returns = nav_series.pct_change().dropna()
        if len(returns) < self.window:
            return current

        rolling = returns.rolling(self.window, min_periods=self.window // 2).std() * math.sqrt(periods_per_year)
        valid = rolling.dropna()
        if valid.empty:
            return current

        cur_vol = float(rolling.iloc[-1])
        low_thresh = float(np.nanpercentile(valid, self.low_pct))
        high_thresh = float(np.nanpercentile(valid, self.high_pct))
        h = self.hysteresis

        if current == "LOW":
            if cur_vol > high_thresh:
                return "HIGH"
            if cur_vol > low_thresh * (1 + h):
                return "MEDIUM"
            return "LOW"
        elif current == "HIGH":
            if cur_vol < low_thresh:
                return "LOW"
            if cur_vol < high_thresh * (1 - h):
                return "MEDIUM"
            return "HIGH"
        else:  # MEDIUM
            if cur_vol < low_thresh:
                return "LOW"
            if cur_vol > high_thresh:
                return "HIGH"
            return "MEDIUM"


# ---------------------------------------------------------------------------
# CorrelationWatchdogModel
# ---------------------------------------------------------------------------

class CorrelationWatchdogModel(RiskModel):
    """
    Monitors average pairwise return correlation across the universe.

    When assets are all moving together (systemic risk / crisis), position
    diversification breaks down. Cuts all weights by 40% when triggered.

    Stateless — recalculated each rebalance.
    """

    def __init__(
        self,
        window: int = 20,
        threshold: float = CORRELATION_THRESHOLD,
        scalar_when_triggered: float = _CORR_SCALAR,
    ):
        self.window = window
        self.threshold = threshold
        self.scalar = scalar_when_triggered

    @property
    def name(self) -> str:
        return "correlation_watchdog"

    def adjust(
        self,
        weights: Dict[str, float],
        portfolio,
        price_data: Dict[str, pd.DataFrame],
        prices: Dict[str, float],
        profile: Dict[str, Any],
    ) -> Tuple[Dict[str, float], Dict[str, Any]]:
        triggered, avg_corr = self._check(price_data)

        if triggered:
            logger.warning(
                "Correlation watchdog triggered: avg_corr=%.3f > %.2f — cutting positions by %.0f%%",
                avg_corr, self.threshold, (1 - self.scalar) * 100,
            )

        adjusted = (
            {sym: w * self.scalar for sym, w in weights.items()}
            if triggered else dict(weights)
        )

        return adjusted, {
            "correlation_triggered": triggered,
            "avg_correlation": round(avg_corr, 4),
            "corr_scalar": self.scalar if triggered else 1.0,
        }

    def _check(self, price_data: Dict[str, pd.DataFrame]) -> Tuple[bool, float]:
        if len(price_data) < 2:
            return False, 0.0

        closes = pd.DataFrame(
            {sym: df["close"] for sym, df in price_data.items() if "close" in df.columns}
        ).ffill()

        if len(closes) < self.window:
            return False, 0.0

        returns = closes.iloc[-self.window:].pct_change().dropna()
        if returns.shape[0] < 2:
            return False, 0.0

        corr = returns.corr()
        n = len(corr)
        upper = [
            corr.iloc[i, j]
            for i in range(n) for j in range(i + 1, n)
            if not math.isnan(corr.iloc[i, j])
        ]
        if not upper:
            return False, 0.0

        avg_corr = float(np.mean(upper))
        return avg_corr > self.threshold, avg_corr


# ---------------------------------------------------------------------------
# EngineRiskModel — compatibility shim (delegates to the three above)
# ---------------------------------------------------------------------------

class EngineRiskModel(RiskModel):
    """
    Phase 2 monolith — now a thin shim over the three composable models.

    Kept for backward compatibility. Prefer building a CompositeRiskModel
    directly with DrawdownCircuitBreakerModel + VolRegimeModel +
    CorrelationWatchdogModel for per-model configurability.
    """

    def __init__(self):
        self._inner = CompositeRiskModel([
            DrawdownCircuitBreakerModel(),
            VolRegimeModel(),
            CorrelationWatchdogModel(),
        ])

    @property
    def name(self) -> str:
        return "engine_risk"

    def adjust(self, weights, portfolio, price_data, prices, profile):
        return self._inner.adjust(weights, portfolio, price_data, prices, profile)
