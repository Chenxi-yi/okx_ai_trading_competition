"""
Online recalibration module.

Provides slow-moving parameter adaptation for live trading:
- Periodic re-optimization on expanding or rolling windows
- Constrained parameter drift (new params can't deviate too far from current)
- Regime detection to switch between parameter sets
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from backtest.metrics import annualised_volatility, daily_returns
from optimize.param_space import ParamSpace, get_param_space, get_param_spaces
from optimize.walk_forward import (
    WalkForwardOptimizer,
    _build_portfolio_strategy,
    _run_backtest_for_period,
    OBJECTIVE_FUNCS,
)
from config.profiles import get_profile
from config.settings import INITIAL_CAPITAL, TRADING_MODE

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Regime detection
# ---------------------------------------------------------------------------

@dataclass
class RegimeState:
    """Tracks the current market regime for parameter set selection."""

    regime: str = "normal"  # "normal", "high_vol", "low_vol", "trending", "mean_reverting"
    confidence: float = 0.5
    last_updated: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"regime": self.regime, "confidence": self.confidence, "last_updated": self.last_updated}


def detect_regime(
    nav_series: pd.Series,
    price_data: Dict[str, pd.DataFrame],
    vol_lookback: int = 60,
    vol_high_pct: float = 75,
    vol_low_pct: float = 25,
    trend_lookback: int = 90,
) -> RegimeState:
    """
    Detect the current market regime based on volatility and trend characteristics.

    Returns a RegimeState with one of:
    - 'high_vol': realized vol is above the 75th percentile of its own history
    - 'low_vol': realized vol is below the 25th percentile
    - 'trending': most assets moving in the same direction
    - 'mean_reverting': assets showing reversal patterns
    - 'normal': none of the above
    """
    if nav_series is None or len(nav_series) < vol_lookback * 2:
        return RegimeState(regime="normal", confidence=0.5, last_updated=str(datetime.now().date()))

    rets = daily_returns(nav_series)
    rolling_vol = rets.rolling(vol_lookback).std() * np.sqrt(365)

    if len(rolling_vol.dropna()) < vol_lookback:
        return RegimeState(regime="normal", confidence=0.5, last_updated=str(datetime.now().date()))

    current_vol = rolling_vol.iloc[-1]
    vol_high = rolling_vol.quantile(vol_high_pct / 100)
    vol_low = rolling_vol.quantile(vol_low_pct / 100)

    # Check trend alignment across assets
    trend_scores = []
    for sym, df in price_data.items():
        if "close" in df.columns and len(df) >= trend_lookback:
            ret = df["close"].pct_change(trend_lookback).iloc[-1]
            if not np.isnan(ret):
                trend_scores.append(np.sign(ret))

    trend_alignment = abs(np.mean(trend_scores)) if trend_scores else 0.0

    # Determine regime
    if current_vol > vol_high:
        return RegimeState(regime="high_vol", confidence=min(0.9, current_vol / vol_high), last_updated=str(datetime.now().date()))
    elif current_vol < vol_low:
        return RegimeState(regime="low_vol", confidence=min(0.9, vol_low / max(current_vol, 1e-8)), last_updated=str(datetime.now().date()))
    elif trend_alignment > 0.7:
        return RegimeState(regime="trending", confidence=trend_alignment, last_updated=str(datetime.now().date()))
    elif trend_alignment < 0.2 and len(trend_scores) >= 4:
        return RegimeState(regime="mean_reverting", confidence=1.0 - trend_alignment, last_updated=str(datetime.now().date()))
    else:
        return RegimeState(regime="normal", confidence=0.5, last_updated=str(datetime.now().date()))


# ---------------------------------------------------------------------------
# Parameter drift constraint
# ---------------------------------------------------------------------------

def constrain_drift(
    new_params: Dict[str, Any],
    current_params: Dict[str, Any],
    max_drift_pct: float = 0.30,
) -> Dict[str, Any]:
    """
    Constrain new parameters so they don't drift more than max_drift_pct
    from the current parameters (for numeric values only).

    List-valued params are not constrained (they're discrete choices).
    """
    constrained = {}
    for key, new_val in new_params.items():
        old_val = current_params.get(key)

        if old_val is None or not isinstance(new_val, (int, float)) or not isinstance(old_val, (int, float)):
            constrained[key] = new_val
            continue

        if old_val == 0:
            constrained[key] = new_val
            continue

        max_change = abs(old_val) * max_drift_pct
        clamped = np.clip(new_val, old_val - max_change, old_val + max_change)

        # Preserve int type if original was int
        if isinstance(old_val, int) and isinstance(new_val, int):
            constrained[key] = int(round(clamped))
        else:
            constrained[key] = float(clamped)

    return constrained


# ---------------------------------------------------------------------------
# Recalibration state persistence
# ---------------------------------------------------------------------------

@dataclass
class RecalibrationState:
    """Persisted state for the recalibration system."""

    current_params: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    regime_params: Dict[str, Dict[str, Dict[str, Any]]] = field(default_factory=dict)
    regime: RegimeState = field(default_factory=RegimeState)
    last_recalibration: Optional[str] = None
    recalibration_history: List[Dict[str, Any]] = field(default_factory=list)

    def save(self, path: Path) -> None:
        data = {
            "current_params": self.current_params,
            "regime_params": self.regime_params,
            "regime": self.regime.to_dict(),
            "last_recalibration": self.last_recalibration,
            "recalibration_history": self.recalibration_history[-50:],  # keep last 50
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, default=str))
        logger.info("Saved recalibration state to %s", path)

    @classmethod
    def load(cls, path: Path) -> "RecalibrationState":
        if not path.exists():
            return cls()
        data = json.loads(path.read_text())
        state = cls(
            current_params=data.get("current_params", {}),
            regime_params=data.get("regime_params", {}),
            last_recalibration=data.get("last_recalibration"),
            recalibration_history=data.get("recalibration_history", []),
        )
        regime_data = data.get("regime", {})
        state.regime = RegimeState(**regime_data) if regime_data else RegimeState()
        return state


@dataclass
class RecalibrationDecision:
    target: str
    candidate_params: Dict[str, Any]
    deployed_params: Dict[str, Any]
    incumbent_params: Dict[str, Any]
    candidate_holdout_score: float
    incumbent_holdout_score: float
    accepted: bool
    reason: str


# ---------------------------------------------------------------------------
# Online recalibrator
# ---------------------------------------------------------------------------

class OnlineRecalibrator:
    """
    Periodic recalibration engine for live trading.

    Usage:
        recal = OnlineRecalibrator(targets=["portfolio_weights", "trend_momentum"])
        new_params = recal.recalibrate(price_data, nav_series, current_date="2025-01-01")
        # new_params is a dict of {target_name: best_params}
    """

    def __init__(
        self,
        targets: List[str] | None = None,
        objective: str = "sharpe",
        mode: str = TRADING_MODE,
        profile_name: str = "daily",
        lookback_months: int = 24,
        max_combos: int = 50,
        max_drift_pct: float = 0.30,
        acceptance_months: int = 3,
        min_improvement: float = 0.05,
        state_path: Optional[Path] = None,
    ):
        self.targets = targets or ["portfolio_weights"]
        self.objective = objective
        self.mode = mode
        self.profile_name = profile_name
        self.profile = get_profile(profile_name)
        self.spaces = get_param_spaces(profile_name)
        self.lookback_months = lookback_months
        self.max_combos = max_combos
        self.max_drift_pct = max_drift_pct
        self.acceptance_months = acceptance_months
        self.min_improvement = min_improvement
        self.state_path = state_path or Path("data/cache/recalibration_state.json")
        self.state = RecalibrationState.load(self.state_path)

    def _split_recalibration_window(
        self,
        current_date: str,
    ) -> tuple[str, str, str, str]:
        end_ts = pd.Timestamp(current_date)
        holdout_months = min(self.acceptance_months, max(1, self.lookback_months - 1))
        holdout_start = end_ts - pd.DateOffset(months=holdout_months) + pd.Timedelta(days=1)
        train_end = holdout_start - pd.Timedelta(days=1)
        train_start = end_ts - pd.DateOffset(months=self.lookback_months)
        return (
            str(train_start.date()),
            str(train_end.date()),
            str(holdout_start.date()),
            str(end_ts.date()),
        )

    def should_recalibrate(
        self,
        current_date: str,
        min_days_between: int = 30,
    ) -> bool:
        """Check if enough time has passed since last recalibration."""
        if self.state.last_recalibration is None:
            return True
        last = pd.Timestamp(self.state.last_recalibration)
        now = pd.Timestamp(current_date)
        return (now - last).days >= min_days_between

    def recalibrate(
        self,
        price_data: Dict[str, pd.DataFrame],
        nav_series: Optional[pd.Series] = None,
        current_date: Optional[str] = None,
    ) -> Dict[str, RecalibrationDecision]:
        """
        Run recalibration for all targets.

        Parameters
        ----------
        price_data : current price data
        nav_series : portfolio NAV history (for regime detection)
        current_date : the current date (defaults to last date in price data)

        Returns
        -------
        Dict mapping target names to recalibration decisions.
        """
        if current_date is None:
            # Use the last date from price data
            dates = []
            for df in price_data.values():
                if not df.empty:
                    dates.append(df.index[-1])
            current_date = str(max(dates).date()) if dates else str(datetime.now().date())

        # Detect regime
        if nav_series is not None:
            self.state.regime = detect_regime(nav_series, price_data)
            logger.info("Detected regime: %s (confidence=%.2f)", self.state.regime.regime, self.state.regime.confidence)

        train_start, train_end, holdout_start, holdout_end = self._split_recalibration_window(current_date)

        results = {}
        for target in self.targets:
            if target not in self.spaces:
                logger.warning("Unknown target '%s', skipping", target)
                continue

            space = get_param_space(target, self.profile_name)
            combos = space.sample(self.max_combos) if space.num_combinations > self.max_combos else space.grid()
            obj_func = OBJECTIVE_FUNCS[self.objective]

            best_score = -np.inf
            best_params = combos[0] if combos else {}

            for params in combos:
                strategy = self._build_strategy(target, price_data, params)
                metrics = _run_backtest_for_period(
                    strategy,
                    price_data,
                    self.mode,
                    train_start,
                    train_end,
                    INITIAL_CAPITAL,
                    profile_name=self.profile_name,
                )
                score = obj_func(metrics)
                if score > best_score:
                    best_score = score
                    best_params = params

            # Apply drift constraint if we have existing params
            current = self.state.current_params.get(target, {})
            if current:
                best_params = constrain_drift(best_params, current, self.max_drift_pct)
                logger.info("Applied drift constraint for %s (max %.0f%%)", target, self.max_drift_pct * 100)

            incumbent_params = current or space.defaults
            candidate_strategy = self._build_strategy(target, price_data, best_params)
            incumbent_strategy = self._build_strategy(target, price_data, incumbent_params)

            candidate_holdout = _run_backtest_for_period(
                candidate_strategy,
                price_data,
                self.mode,
                holdout_start,
                holdout_end,
                INITIAL_CAPITAL,
                profile_name=self.profile_name,
            )
            incumbent_holdout = _run_backtest_for_period(
                incumbent_strategy,
                price_data,
                self.mode,
                holdout_start,
                holdout_end,
                INITIAL_CAPITAL,
                profile_name=self.profile_name,
            )
            candidate_holdout_score = obj_func(candidate_holdout)
            incumbent_holdout_score = obj_func(incumbent_holdout)

            accepted = candidate_holdout_score >= incumbent_holdout_score + self.min_improvement
            deployed_params = best_params if accepted else incumbent_params
            reason = (
                f"accepted on untouched holdout ({candidate_holdout_score:.4f} vs {incumbent_holdout_score:.4f})"
                if accepted
                else f"rejected on untouched holdout ({candidate_holdout_score:.4f} vs {incumbent_holdout_score:.4f})"
            )

            # Store regime-specific params
            regime_key = self.state.regime.regime
            if regime_key not in self.state.regime_params:
                self.state.regime_params[regime_key] = {}
            self.state.regime_params[regime_key][target] = deployed_params

            self.state.current_params[target] = deployed_params
            results[target] = RecalibrationDecision(
                target=target,
                candidate_params=best_params,
                deployed_params=deployed_params,
                incumbent_params=incumbent_params,
                candidate_holdout_score=candidate_holdout_score,
                incumbent_holdout_score=incumbent_holdout_score,
                accepted=accepted,
                reason=reason,
            )

            logger.info(
                "Recalibrated %s candidate=%s train_score=%.4f holdout_new=%.4f holdout_old=%.4f accepted=%s",
                target, best_params, best_score, candidate_holdout_score, incumbent_holdout_score, accepted,
            )

        # Update state
        self.state.last_recalibration = current_date
        self.state.recalibration_history.append({
            "date": current_date,
            "regime": self.state.regime.to_dict(),
            "params": {k: str(v.deployed_params) for k, v in results.items()},
            "accepted": {k: v.accepted for k, v in results.items()},
            "holdout_window": {"start": holdout_start, "end": holdout_end},
        })
        self.state.save(self.state_path)

        return results

    def get_regime_params(self, target: str) -> Optional[Dict[str, Any]]:
        """Get the best params for the current regime, if available."""
        regime_key = self.state.regime.regime
        regime_params = self.state.regime_params.get(regime_key, {})
        return regime_params.get(target)

    def _build_strategy(
        self,
        target: str,
        price_data: Dict[str, pd.DataFrame],
        overrides: Dict[str, Any],
    ) -> Any:
        """Build strategy with overrides (reuses WFO logic)."""
        space = get_param_space(target, self.profile_name)
        if target == "portfolio_weights":
            return _build_portfolio_strategy(
                price_data,
                self.mode,
                profile_name=self.profile_name,
                portfolio_weights=overrides,
            )
        cfg = space.make_config(overrides)
        if target == "trend_momentum":
            return _build_portfolio_strategy(price_data, self.mode, profile_name=self.profile_name, trend_cfg=cfg)
        elif target == "cross_sectional":
            return _build_portfolio_strategy(price_data, self.mode, profile_name=self.profile_name, cs_cfg=cfg)
        elif target == "funding_carry":
            return _build_portfolio_strategy(price_data, self.mode, profile_name=self.profile_name, carry_cfg=cfg)
        raise ValueError(f"Unknown target: {target}")


# ---------------------------------------------------------------------------
# Pretty printing
# ---------------------------------------------------------------------------

def print_recalibration_result(
    results: Dict[str, RecalibrationDecision],
    state: RecalibrationState,
) -> None:
    """Print recalibration summary."""
    print(f"\n{'='*55}")
    print(f"  RECALIBRATION RESULTS")
    print(f"  Date: {state.last_recalibration}")
    print(f"  Regime: {state.regime.regime} (confidence={state.regime.confidence:.2f})")
    print(f"{'='*55}")

    for target, decision in results.items():
        print(f"\n  {target}:")
        print(f"    accepted: {decision.accepted}")
        print(f"    reason: {decision.reason}")
        print(f"    candidate_holdout_score: {decision.candidate_holdout_score:.4f}")
        print(f"    incumbent_holdout_score: {decision.incumbent_holdout_score:.4f}")
        print(f"    deployed_params: {decision.deployed_params}")
        if decision.deployed_params != decision.candidate_params:
            print(f"    candidate_params: {decision.candidate_params}")

    if state.recalibration_history and len(state.recalibration_history) > 1:
        print(f"\n{'─'*55}")
        print(f"  Recalibration History (last 5):")
        for entry in state.recalibration_history[-5:]:
            print(f"    {entry['date']}: regime={entry['regime']['regime']}")

    print(f"{'='*55}\n")
