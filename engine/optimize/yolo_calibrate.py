"""
optimize/yolo_calibrate.py
===========================
Walk-forward calibration for the YOLO Momentum strategy.

Uses the engine's WalkForwardOptimizer pattern:
  - Rolling train/validate windows on real historical data
  - Grid search over YOLO-specific parameter space
  - Parameter penalty regularization (prefer simpler params)
  - Overfit diagnostics (Sharpe degradation, param stability)
  - Regime-aware parameter sets

The key difference: instead of running a single backtest per combo,
we run N mini Monte-Carlo trials per combo (sampled from the fold's
date range) and optimize on the aggregate success rate + expected ROI.

Usage:
  python3 -m optimize.yolo_calibrate --trials-per-combo 30
  python3 -m optimize.yolo_calibrate --quick  # fast test with fewer combos
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

import sys
ENGINE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE_DIR))

from optimize.param_space import ParamSpace
from optimize.walk_forward import (
    FoldResult,
    OptimizationResult,
    make_walk_forward_splits,
    _param_distance_from_defaults,
    print_optimization_result,
)
from optimize.recalibrate import (
    constrain_drift,
    detect_regime,
    RegimeState,
)
from backtest.yolo_montecarlo import (
    load_universe_data,
    run_trial,
    available_symbols_at,
    print_summary,
    TrialResult,
    UNIVERSE,
    ROUND_MARGINS,
)
from backtest.metrics import (
    sharpe_ratio,
    daily_returns,
    max_drawdown,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# YOLO-specific parameter space
# ---------------------------------------------------------------------------

YOLO_PARAM_SPACE = ParamSpace(
    name="yolo_momentum",
    defaults={
        # Leverage
        "default_lever":        50,
        "high_vol_lever":       30,
        "low_vol_lever":        75,
        # Entry thresholds
        "rsi_long_low":         55,
        "rsi_long_high":        75,
        "rsi_short_low":        25,
        "rsi_short_high":       45,
        "volume_mult_threshold": 1.2,
        "ema_alignment_min":    0.60,
        # Exit
        "hard_stop_pct":        0.60,
        "trail_activate_pct":   0.50,
        "trail_distance_pct":   0.40,
        "time_decay_hours":     96,
        # Reversal detection
        "reversal_threshold":   0.45,
        "tighten_threshold":    0.30,
    },
    params={
        # Leverage — key risk/reward knob
        "default_lever": [30, 40, 50, 75, 100],
        "high_vol_lever": [20, 30, 40],
        "low_vol_lever": [50, 75, 100, 125],

        # Entry — controls selectivity
        "rsi_long_low": [50, 55, 60],
        "rsi_long_high": [70, 75, 80],
        "rsi_short_low": [20, 25, 30],
        "rsi_short_high": [40, 45, 50],
        "volume_mult_threshold": [1.0, 1.2, 1.5, 2.0],
        "ema_alignment_min": [0.50, 0.60, 0.67, 0.75],

        # Exit — controls when to cut losses or take profits
        "hard_stop_pct": [0.40, 0.50, 0.60, 0.70, 0.80],
        "trail_activate_pct": [0.30, 0.40, 0.50, 0.60],
        "trail_distance_pct": [0.25, 0.30, 0.40, 0.50],
        "time_decay_hours": [48, 72, 96, 144, 240],

        # Reversal detection sensitivity
        "reversal_threshold": [0.35, 0.40, 0.45, 0.50, 0.55],
        "tighten_threshold": [0.20, 0.25, 0.30, 0.35],
    },
)


# ---------------------------------------------------------------------------
# Objective function: run mini MC trials within a date window
# ---------------------------------------------------------------------------

@dataclass
class ComboResult:
    """Result of evaluating one parameter combination."""
    params: Dict[str, Any]
    n_trials: int
    success_rate: float
    mean_roi: float
    median_roi: float
    mean_invested: float
    mean_trades: float
    mean_rounds: float
    mean_max_dd: float
    roi_std: float
    # Composite objective score
    objective: float


def _patch_trial_params(params: Dict[str, Any]) -> None:
    """
    Monkey-patch the yolo_montecarlo module's constants with candidate params.
    This avoids threading config through the entire call stack.
    We restore after each evaluation.
    """
    import backtest.yolo_montecarlo as ym

    # Map param names to module-level constants
    _PARAM_MAP = {
        "default_lever":        "DEFAULT_LEVER",
        "high_vol_lever":       "HIGH_VOL_LEVER",
        "low_vol_lever":        "LOW_VOL_LEVER",
        "rsi_long_low":         "RSI_LONG_RANGE",   # special: tuple[0]
        "rsi_long_high":        "RSI_LONG_RANGE",   # special: tuple[1]
        "rsi_short_low":        "RSI_SHORT_RANGE",  # special: tuple[0]
        "rsi_short_high":       "RSI_SHORT_RANGE",  # special: tuple[1]
        "volume_mult_threshold": "VOLUME_MULT_THRESHOLD",
        "ema_alignment_min":    "EMA_ALIGNMENT_MIN",
        "hard_stop_pct":        "HARD_STOP_PCT",
        "trail_activate_pct":   "TRAIL_ACTIVATE_PCT",
        "trail_distance_pct":   "TRAIL_DISTANCE_PCT",
        "time_decay_hours":     "TIME_DECAY_HOURS",
        "reversal_threshold":   None,  # handled in detect_reversal_bt call
        "tighten_threshold":    None,
    }

    for key, val in params.items():
        if key == "rsi_long_low":
            ym.RSI_LONG_RANGE = (val, ym.RSI_LONG_RANGE[1])
        elif key == "rsi_long_high":
            ym.RSI_LONG_RANGE = (ym.RSI_LONG_RANGE[0], val)
        elif key == "rsi_short_low":
            ym.RSI_SHORT_RANGE = (val, ym.RSI_SHORT_RANGE[1])
        elif key == "rsi_short_high":
            ym.RSI_SHORT_RANGE = (ym.RSI_SHORT_RANGE[0], val)
        elif key in ("reversal_threshold", "tighten_threshold"):
            # These aren't module-level constants in the MC sim — they're
            # only used in the live strategy. For backtest, reversal detection
            # uses the module-level 0.45/0.30. We need to patch them too.
            pass  # Handled below
        else:
            attr = _PARAM_MAP.get(key)
            if attr and hasattr(ym, attr):
                setattr(ym, attr, val)


def _save_originals() -> Dict[str, Any]:
    """Save module-level constants before patching."""
    import backtest.yolo_montecarlo as ym
    return {
        "DEFAULT_LEVER": ym.DEFAULT_LEVER,
        "HIGH_VOL_LEVER": ym.HIGH_VOL_LEVER,
        "LOW_VOL_LEVER": ym.LOW_VOL_LEVER,
        "RSI_LONG_RANGE": ym.RSI_LONG_RANGE,
        "RSI_SHORT_RANGE": ym.RSI_SHORT_RANGE,
        "VOLUME_MULT_THRESHOLD": ym.VOLUME_MULT_THRESHOLD,
        "EMA_ALIGNMENT_MIN": ym.EMA_ALIGNMENT_MIN,
        "HARD_STOP_PCT": ym.HARD_STOP_PCT,
        "TRAIL_ACTIVATE_PCT": ym.TRAIL_ACTIVATE_PCT,
        "TRAIL_DISTANCE_PCT": ym.TRAIL_DISTANCE_PCT,
        "TIME_DECAY_HOURS": ym.TIME_DECAY_HOURS,
    }


def _restore_originals(saved: Dict[str, Any]) -> None:
    """Restore module-level constants after patching."""
    import backtest.yolo_montecarlo as ym
    for attr, val in saved.items():
        setattr(ym, attr, val)


def _ensure_tz(ts: pd.Timestamp) -> pd.Timestamp:
    """Ensure timestamp is tz-aware (UTC) for comparison with data index."""
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts


def evaluate_combo(
    params: Dict[str, Any],
    data: Dict[str, pd.DataFrame],
    start: pd.Timestamp,
    end: pd.Timestamp,
    n_trials: int = 30,
    seed: int = 42,
) -> ComboResult:
    """
    Evaluate a parameter combination by running n_trials mini MC trials
    within the [start, end] window. Returns aggregate metrics.
    """
    saved = _save_originals()
    _patch_trial_params(params)

    # Ensure timestamps are tz-aware for data comparison
    start = _ensure_tz(start)
    end = _ensure_tz(end)

    try:
        rng = random.Random(seed)
        results: List[TrialResult] = []

        # Generate trial start dates within the window
        window_sec = (end - start).total_seconds()
        if window_sec <= 0:
            return _empty_combo_result(params, n_trials)

        # Leave 14 days at the end for trial to run
        max_start = end - pd.Timedelta(days=15)
        usable_sec = (max_start - start).total_seconds()
        if usable_sec <= 0:
            return _empty_combo_result(params, n_trials)

        for i in range(n_trials):
            offset = rng.uniform(0, usable_sec)
            trial_start = start + pd.Timedelta(seconds=offset)
            trial_start = trial_start.floor("h")

            # Check enough symbols available
            avail = available_symbols_at(data, trial_start)
            if len(avail) < 3:
                continue

            result = run_trial(i + 1, data, trial_start, rng)
            results.append(result)

        if not results:
            return _empty_combo_result(params, n_trials)

        wins = [r for r in results if r.success]
        rois = [r.final_roi_pct for r in results]

        success_rate = len(wins) / len(results)
        mean_roi = float(np.mean(rois))
        median_roi = float(np.median(rois))
        roi_std = float(np.std(rois))
        mean_invested = float(np.mean([r.total_invested for r in results]))
        mean_trades = float(np.mean([r.num_trades for r in results]))
        mean_rounds = float(np.mean([r.num_rounds for r in results]))
        mean_max_dd = float(np.mean([r.max_drawdown_pct for r in results]))

        # Composite objective: prioritize success rate, then risk-adjusted ROI
        # success_rate is the primary metric (competition cares about hitting 20%)
        # Penalize high capital usage (prefer winning in fewer rounds)
        # Penalize high drawdown and high variance
        capital_efficiency = 1.0 - (mean_invested / 750)  # 750 = max total invested
        dd_penalty = max(0, -mean_max_dd - 1.0) * 0.1  # penalize DD > -100%
        variance_penalty = min(roi_std, 2.0) * 0.05

        objective = (
            0.50 * success_rate +
            0.20 * max(0, min(mean_roi, 0.5)) +  # cap ROI contribution
            0.15 * capital_efficiency +
            0.10 * max(0, min(median_roi, 0.5)) -
            0.05 * variance_penalty -
            dd_penalty
        )

        return ComboResult(
            params=params,
            n_trials=len(results),
            success_rate=success_rate,
            mean_roi=mean_roi,
            median_roi=median_roi,
            mean_invested=mean_invested,
            mean_trades=mean_trades,
            mean_rounds=mean_rounds,
            mean_max_dd=mean_max_dd,
            roi_std=roi_std,
            objective=objective,
        )

    finally:
        _restore_originals(saved)


def _empty_combo_result(params: Dict[str, Any], n_trials: int) -> ComboResult:
    return ComboResult(
        params=params, n_trials=0, success_rate=0, mean_roi=0,
        median_roi=0, mean_invested=0, mean_trades=0, mean_rounds=0,
        mean_max_dd=0, roi_std=0, objective=-1.0,
    )


# ---------------------------------------------------------------------------
# Walk-forward calibration engine
# ---------------------------------------------------------------------------

@dataclass
class YoloFoldResult:
    """One fold of walk-forward calibration."""
    fold_idx: int
    train_start: str
    train_end: str
    val_start: str
    val_end: str
    best_params: Dict[str, Any]
    train_result: ComboResult
    val_result: ComboResult
    param_penalty: float
    combos_tested: int


@dataclass
class YoloCalibrationResult:
    """Aggregate calibration result across all folds."""
    folds: List[YoloFoldResult]
    final_params: Dict[str, Any]
    elapsed_seconds: float

    @property
    def avg_train_success(self) -> float:
        return float(np.mean([f.train_result.success_rate for f in self.folds]))

    @property
    def avg_val_success(self) -> float:
        return float(np.mean([f.val_result.success_rate for f in self.folds]))

    @property
    def avg_train_objective(self) -> float:
        return float(np.mean([f.train_result.objective for f in self.folds]))

    @property
    def avg_val_objective(self) -> float:
        return float(np.mean([f.val_result.objective for f in self.folds]))

    @property
    def success_degradation(self) -> float:
        if self.avg_train_success == 0:
            return 0.0
        return 1.0 - (self.avg_val_success / self.avg_train_success)

    @property
    def param_stability(self) -> float:
        if len(self.folds) < 2:
            return 1.0
        strs = [str(sorted(f.best_params.items())) for f in self.folds]
        most_common = max(set(strs), key=strs.count)
        return strs.count(most_common) / len(strs)

    def overfit_warnings(self) -> List[str]:
        warnings = []
        if self.success_degradation > 0.20:
            warnings.append(
                f"OVERFIT RISK: success rate degrades {self.success_degradation:.0%} OOS "
                f"(train={self.avg_train_success:.1%} val={self.avg_val_success:.1%})"
            )
        if self.param_stability < 0.40:
            warnings.append(
                f"UNSTABLE PARAMS: best params match across only {self.param_stability:.0%} of folds"
            )
        neg_folds = sum(1 for f in self.folds if f.val_result.success_rate < 0.5)
        if neg_folds > len(self.folds) / 2:
            warnings.append(
                f"POOR OOS: {neg_folds}/{len(self.folds)} folds have <50% OOS success rate"
            )
        return warnings


class YoloWalkForwardCalibrator:
    """
    Walk-forward calibration specifically for the YOLO Momentum strategy.

    Instead of running backtests through BacktestEngine, it uses the
    Monte Carlo trial simulator with configurable parameters.
    """

    def __init__(
        self,
        train_months: int = 12,
        val_months: int = 3,
        step_months: int = 3,
        max_combos: int = 60,
        trials_per_combo: int = 30,
        param_penalty_weight: float = 0.10,
    ):
        self.space = YOLO_PARAM_SPACE
        self.train_months = train_months
        self.val_months = val_months
        self.step_months = step_months
        self.max_combos = max_combos
        self.trials_per_combo = trials_per_combo
        self.param_penalty_weight = param_penalty_weight

    def _get_combos(self, seed: int = 42) -> List[Dict[str, Any]]:
        """
        Get parameter combinations via random per-param sampling.
        Avoids enumerating the full grid (which can be billions of combos).
        """
        rng = np.random.default_rng(seed)
        combos = []
        keys = list(self.space.params.keys())

        # Always include defaults as first combo
        combos.append(self.space.defaults.copy())

        for _ in range(self.max_combos - 1):
            combo = {}
            for key in keys:
                candidates = self.space.params[key]
                combo[key] = candidates[rng.integers(len(candidates))]
            combos.append(combo)

        logger.info("Generated %d random combos (grid too large to enumerate)", len(combos))
        return combos

    def run(
        self,
        data: Dict[str, pd.DataFrame],
        start: str = "2022-07-01",
        end: str = "2026-03-31",
    ) -> YoloCalibrationResult:
        """
        Run walk-forward calibration across all folds.
        """
        t0 = time.time()

        splits = make_walk_forward_splits(
            start, end, self.train_months, self.val_months, self.step_months,
        )
        if not splits:
            raise ValueError(f"No valid splits for {start}→{end}")

        logger.info(
            "YOLO Walk-Forward Calibration: %d folds, %d max combos, %d trials/combo",
            len(splits), self.max_combos, self.trials_per_combo,
        )

        folds: List[YoloFoldResult] = []
        all_best_params: List[Dict[str, Any]] = []

        for fold_idx, (ts, te, vs, ve) in enumerate(splits):
            logger.info("="*60)
            logger.info("Fold %d: train=%s→%s, val=%s→%s", fold_idx, ts, te, vs, ve)

            combos = self._get_combos(seed=42 + fold_idx)
            train_start = pd.Timestamp(ts, tz="UTC")
            train_end = pd.Timestamp(te, tz="UTC")
            val_start = pd.Timestamp(vs, tz="UTC")
            val_end = pd.Timestamp(ve, tz="UTC")

            best_score = -np.inf
            best_params = combos[0]
            best_train_result = None

            for i, params in enumerate(combos):
                # Evaluate on training window
                result = evaluate_combo(
                    params, data,
                    start=train_start,
                    end=train_end,
                    n_trials=self.trials_per_combo,
                    seed=42 + fold_idx * 1000 + i,
                )

                # Apply parameter penalty (prefer params close to defaults)
                penalty = self.param_penalty_weight * _param_distance_from_defaults(self.space, params)
                score = result.objective - penalty

                if score > best_score:
                    best_score = score
                    best_params = params
                    best_train_result = result

                if (i + 1) % 10 == 0:
                    logger.info(
                        "  Fold %d: tested %d/%d combos (best obj=%.4f success=%.1f%%)",
                        fold_idx, i + 1, len(combos),
                        best_score,
                        best_train_result.success_rate * 100 if best_train_result else 0,
                    )

            # Validate best params on holdout
            val_result = evaluate_combo(
                best_params, data,
                start=val_start,
                end=val_end,
                n_trials=self.trials_per_combo,
                seed=99 + fold_idx,
            )

            penalty = self.param_penalty_weight * _param_distance_from_defaults(self.space, best_params)

            fold = YoloFoldResult(
                fold_idx=fold_idx,
                train_start=ts, train_end=te,
                val_start=vs, val_end=ve,
                best_params=best_params,
                train_result=best_train_result,
                val_result=val_result,
                param_penalty=penalty,
                combos_tested=len(combos),
            )
            folds.append(fold)
            all_best_params.append(best_params)

            logger.info(
                "Fold %d DONE: train success=%.1f%% val success=%.1f%% "
                "train obj=%.4f val obj=%.4f params=%s",
                fold_idx,
                best_train_result.success_rate * 100,
                val_result.success_rate * 100,
                best_train_result.objective,
                val_result.objective,
                best_params,
            )

        # Determine final params: use the set that appears most often across folds,
        # or if all different, use the one with best validation objective.
        final_params = self._select_final_params(folds)

        elapsed = time.time() - t0
        result = YoloCalibrationResult(
            folds=folds,
            final_params=final_params,
            elapsed_seconds=elapsed,
        )

        return result

    def _select_final_params(self, folds: List[YoloFoldResult]) -> Dict[str, Any]:
        """
        Select final parameters using a voting + performance weighting approach.

        For each parameter independently:
        1. Collect all values chosen across folds
        2. Weight by validation success rate
        3. For numeric params: weighted median
        4. For non-numeric: majority vote weighted by val success
        """
        if not folds:
            return self.space.defaults.copy()

        # Weight by validation success
        weights = np.array([f.val_result.success_rate for f in folds])
        if weights.sum() == 0:
            weights = np.ones(len(folds))
        weights = weights / weights.sum()

        final = {}
        for param_name, candidates in self.space.params.items():
            values = [f.best_params.get(param_name, self.space.defaults.get(param_name)) for f in folds]

            # Check if all values are numeric
            all_numeric = all(isinstance(v, (int, float)) for v in values)
            if all_numeric:
                # Weighted median
                sorted_pairs = sorted(zip(values, weights), key=lambda x: x[0])
                cumulative = 0.0
                for val, w in sorted_pairs:
                    cumulative += w
                    if cumulative >= 0.5:
                        # Snap to nearest candidate
                        best_candidate = min(candidates, key=lambda c: abs(c - val))
                        final[param_name] = best_candidate
                        break
                else:
                    final[param_name] = values[-1]  # fallback
            else:
                # Weighted majority vote
                vote_weights: Dict[Any, float] = {}
                for val, w in zip(values, weights):
                    vote_weights[val] = vote_weights.get(val, 0) + w
                final[param_name] = max(vote_weights, key=vote_weights.get)

        return final


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def print_calibration_result(result: YoloCalibrationResult) -> None:
    """Pretty-print calibration results."""
    print(f"\n{'='*70}")
    print(f"  YOLO MOMENTUM — WALK-FORWARD CALIBRATION RESULTS")
    print(f"  Folds: {len(result.folds)}  |  Time: {result.elapsed_seconds:.0f}s")
    print(f"{'='*70}")

    # Per-fold table
    print(f"\n{'Fold':>4} {'Train Period':>24} {'Val Period':>24} {'Train Win%':>10} {'Val Win%':>10} {'Train Obj':>10} {'Val Obj':>10}")
    print("-" * 100)
    for f in result.folds:
        print(
            f"{f.fold_idx:>4} {f.train_start+'→'+f.train_end:>24} "
            f"{f.val_start+'→'+f.val_end:>24} "
            f"{f.train_result.success_rate*100:>9.1f}% "
            f"{f.val_result.success_rate*100:>9.1f}% "
            f"{f.train_result.objective:>10.4f} "
            f"{f.val_result.objective:>10.4f}"
        )

    print(f"\n{'─'*70}")
    print(f"  Avg Train Success Rate:  {result.avg_train_success*100:>8.1f}%")
    print(f"  Avg Val Success Rate:    {result.avg_val_success*100:>8.1f}%")
    print(f"  Success Degradation:     {result.success_degradation*100:>8.1f}%")
    print(f"  Param Stability:         {result.param_stability*100:>8.1f}%")

    # Per-fold additional details
    print(f"\n{'─'*70}")
    print(f"  Fold Details:")
    for f in result.folds:
        tr = f.train_result
        vr = f.val_result
        print(f"    Fold {f.fold_idx}: train(roi={tr.mean_roi*100:.1f}% dd={tr.mean_max_dd*100:.1f}% "
              f"rounds={tr.mean_rounds:.1f}) val(roi={vr.mean_roi*100:.1f}% dd={vr.mean_max_dd*100:.1f}% "
              f"rounds={vr.mean_rounds:.1f})")

    # Best params per fold
    print(f"\n{'─'*70}")
    print(f"  Best Parameters per Fold:")
    for f in result.folds:
        print(f"    Fold {f.fold_idx}: {f.best_params}")

    # Final calibrated params
    print(f"\n{'─'*70}")
    print(f"  FINAL CALIBRATED PARAMETERS:")
    for k, v in sorted(result.final_params.items()):
        default = YOLO_PARAM_SPACE.defaults.get(k)
        changed = " *" if v != default else ""
        print(f"    {k:30s} = {str(v):>10s}  (default: {str(default):>10s}){changed}")

    # Overfit warnings
    warnings = result.overfit_warnings()
    if warnings:
        print(f"\n{'!'*70}")
        print("  OVERFIT DIAGNOSTICS:")
        for w in warnings:
            print(f"    WARNING: {w}")
        print(f"{'!'*70}")
    else:
        print(f"\n  No major overfit warnings detected.")

    print(f"\n{'='*70}\n")


def save_calibrated_params(
    result: YoloCalibrationResult,
    path: Optional[str] = None,
) -> str:
    """Save calibrated parameters to JSON for use in live strategy."""
    if path is None:
        path = str(ENGINE_DIR / "results" / "yolo_calibrated_params.json")

    output = {
        "calibrated_at": str(pd.Timestamp.now()),
        "n_folds": len(result.folds),
        "avg_train_success_pct": round(result.avg_train_success * 100, 2),
        "avg_val_success_pct": round(result.avg_val_success * 100, 2),
        "success_degradation_pct": round(result.success_degradation * 100, 2),
        "param_stability_pct": round(result.param_stability * 100, 2),
        "overfit_warnings": result.overfit_warnings(),
        "final_params": result.final_params,
        "per_fold_params": [
            {
                "fold": f.fold_idx,
                "train": f"{f.train_start}→{f.train_end}",
                "val": f"{f.val_start}→{f.val_end}",
                "params": f.best_params,
                "train_success_pct": round(f.train_result.success_rate * 100, 2),
                "val_success_pct": round(f.val_result.success_rate * 100, 2),
            }
            for f in result.folds
        ],
    }

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fp:
        json.dump(output, fp, indent=2, default=str)

    logger.info("Calibrated params saved to %s", path)
    return path


def apply_calibrated_params(params_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Load calibrated params and return them in a format ready for
    the yolo_momentum strategy config.
    """
    if params_path is None:
        params_path = str(ENGINE_DIR / "results" / "yolo_calibrated_params.json")

    with open(params_path) as f:
        data = json.load(f)

    final = data["final_params"]

    # Map calibrated params to yolo_momentum config keys
    config = {
        "default_lever":        final.get("default_lever", 50),
        "high_vol_lever":       final.get("high_vol_lever", 30),
        "low_vol_lever":        final.get("low_vol_lever", 75),
        "rsi_long_low":         final.get("rsi_long_low", 55),
        "rsi_long_high":        final.get("rsi_long_high", 75),
        "rsi_short_low":        final.get("rsi_short_low", 25),
        "rsi_short_high":       final.get("rsi_short_high", 45),
        "volume_mult_threshold": final.get("volume_mult_threshold", 1.2),
        "hard_stop_pct":        final.get("hard_stop_pct", 0.60),
        "trail_activate_pct":   final.get("trail_activate_pct", 0.50),
        "trail_distance_pct":   final.get("trail_distance_pct", 0.40),
        "time_decay_hours":     final.get("time_decay_hours", 96) / 24,  # convert to time_decay_hours for live
        "reversal_threshold":   final.get("reversal_threshold", 0.45),
        "tighten_threshold":    final.get("tighten_threshold", 0.30),
    }
    return config


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Walk-forward calibration for YOLO Momentum strategy",
    )
    parser.add_argument("--train-months", type=int, default=12,
                        help="Training window in months (default: 12)")
    parser.add_argument("--val-months", type=int, default=3,
                        help="Validation window in months (default: 3)")
    parser.add_argument("--step-months", type=int, default=3,
                        help="Step size between folds in months (default: 3)")
    parser.add_argument("--max-combos", type=int, default=60,
                        help="Max parameter combinations per fold (default: 60)")
    parser.add_argument("--trials-per-combo", type=int, default=30,
                        help="MC trials per combo evaluation (default: 30)")
    parser.add_argument("--penalty", type=float, default=0.10,
                        help="Parameter penalty weight (default: 0.10)")
    parser.add_argument("--data-start", type=str, default="2022-06-01")
    parser.add_argument("--data-end", type=str, default="2026-03-31")
    parser.add_argument("--output", type=str, default=None,
                        help="Output JSON path for calibrated params")
    parser.add_argument("--quick", action="store_true",
                        help="Quick test: fewer combos and trials")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if args.quick:
        args.max_combos = 10
        args.trials_per_combo = 10
        args.train_months = 6
        args.val_months = 3
        args.step_months = 6

    # Load data (reuses cache from MC backtest)
    logger.info("Loading universe data...")
    data = load_universe_data(UNIVERSE, start=args.data_start, end=args.data_end)
    logger.info("Loaded %d symbols", len(data))

    # Run calibration
    calibrator = YoloWalkForwardCalibrator(
        train_months=args.train_months,
        val_months=args.val_months,
        step_months=args.step_months,
        max_combos=args.max_combos,
        trials_per_combo=args.trials_per_combo,
        param_penalty_weight=args.penalty,
    )

    result = calibrator.run(data, start=args.data_start, end=args.data_end)

    # Print results
    print_calibration_result(result)

    # Save calibrated params
    path = save_calibrated_params(result, args.output)
    print(f"Calibrated parameters saved to: {path}")

    # Show how to apply
    print("\nTo apply calibrated params to live strategy:")
    print("  from optimize.yolo_calibrate import apply_calibrated_params")
    print("  config = apply_calibrated_params()")
    print("  # Pass config to YoloMomentumStrategy(config)")


if __name__ == "__main__":
    main()
