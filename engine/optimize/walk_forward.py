"""
Walk-forward optimization engine.

Splits historical data into rolling train/validate windows, runs grid search
on the training set, validates on the holdout, and aggregates out-of-sample
performance across all folds. Includes anti-overfit diagnostics.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from backtest.engine import BacktestEngine
from backtest.metrics import (
    annualised_return,
    annualised_volatility,
    calmar_ratio,
    daily_returns,
    infer_periods_per_year,
    max_drawdown,
    sharpe_ratio,
    sortino_ratio,
)
from config.profiles import get_profile
from config.settings import INITIAL_CAPITAL, TRADING_MODE, get_symbols
from data.fetcher import fetch_universe, generate_synthetic_universe
from optimize.param_space import ParamSpace, get_param_space, get_param_spaces
from strategies.base import BaseStrategy
from strategies.factory import build_portfolio_strategy, build_strategy

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class FoldResult:
    fold_idx: int
    train_start: str
    train_end: str
    val_start: str
    val_end: str
    best_params: Dict[str, Any]
    train_sharpe: float
    train_calmar: float
    val_sharpe: float
    val_calmar: float
    val_annual_return: float
    val_max_drawdown: float
    train_objective: float = 0.0
    val_objective: float = 0.0
    param_penalty: float = 0.0
    candidate_count: int = 0
    train_score_gap: float = 0.0
    val_nav: Optional[pd.Series] = None


@dataclass
class OptimizationResult:
    target: str
    objective: str
    folds: List[FoldResult]
    oos_nav: Optional[pd.Series] = None  # concatenated out-of-sample NAV
    elapsed_seconds: float = 0.0

    @property
    def avg_train_sharpe(self) -> float:
        return float(np.mean([f.train_sharpe for f in self.folds]))

    @property
    def avg_val_sharpe(self) -> float:
        return float(np.mean([f.val_sharpe for f in self.folds]))

    @property
    def avg_val_calmar(self) -> float:
        return float(np.mean([f.val_calmar for f in self.folds]))

    @property
    def avg_train_objective(self) -> float:
        return float(np.mean([f.train_objective for f in self.folds]))

    @property
    def avg_val_objective(self) -> float:
        return float(np.mean([f.val_objective for f in self.folds]))

    @property
    def avg_train_score_gap(self) -> float:
        return float(np.mean([f.train_score_gap for f in self.folds]))

    @property
    def avg_param_penalty(self) -> float:
        return float(np.mean([f.param_penalty for f in self.folds]))

    @property
    def sharpe_degradation(self) -> float:
        """How much Sharpe degrades from in-sample to out-of-sample (ratio)."""
        if self.avg_train_sharpe == 0:
            return 0.0
        return 1.0 - (self.avg_val_sharpe / self.avg_train_sharpe)

    @property
    def param_stability(self) -> float:
        """Fraction of folds where the best params match the most common params."""
        if len(self.folds) < 2:
            return 1.0
        param_strs = [str(sorted(f.best_params.items())) for f in self.folds]
        most_common = max(set(param_strs), key=param_strs.count)
        return param_strs.count(most_common) / len(param_strs)

    def overfit_warnings(self) -> List[str]:
        warnings = []
        if self.sharpe_degradation > 0.50:
            warnings.append(
                f"HIGH OVERFIT RISK: Sharpe degrades {self.sharpe_degradation:.0%} "
                f"out-of-sample (train={self.avg_train_sharpe:.2f}, val={self.avg_val_sharpe:.2f})"
            )
        elif self.sharpe_degradation > 0.30:
            warnings.append(
                f"MODERATE OVERFIT RISK: Sharpe degrades {self.sharpe_degradation:.0%} out-of-sample"
            )
        if self.param_stability < 0.50:
            warnings.append(
                f"UNSTABLE PARAMS: Best params match across only {self.param_stability:.0%} of folds"
            )
        neg_folds = sum(1 for f in self.folds if f.val_sharpe < 0)
        if neg_folds > len(self.folds) / 2:
            warnings.append(
                f"POOR OOS: {neg_folds}/{len(self.folds)} folds have negative out-of-sample Sharpe"
            )
        if self.avg_train_score_gap > 0.5:
            warnings.append(
                f"SELECTION BIAS RISK: winning train score exceeds median candidate by {self.avg_train_score_gap:.2f} on average"
            )
        if self.avg_val_objective < 0:
            warnings.append(
                f"NEGATIVE OOS OBJECTIVE: average validation {self.objective} is {self.avg_val_objective:.2f}"
            )
        return warnings


def _coerce_numeric(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float, np.integer, np.floating)):
        return float(value)
    return None


def _param_distance_from_defaults(space: ParamSpace, params: Dict[str, Any]) -> float:
    """
    Normalized distance from defaults in discrete search space.

    This acts as a mild regularizer so the optimizer prefers simpler / less
    extreme settings when performance is similar.
    """
    if not space.params:
        return 0.0

    distances = []
    for key, candidates in space.params.items():
        if not candidates:
            continue

        chosen = params.get(key, space.defaults.get(key))
        default = space.defaults.get(key, candidates[0])

        try:
            chosen_idx = candidates.index(chosen)
        except ValueError:
            chosen_idx = None

        try:
            default_idx = candidates.index(default)
        except ValueError:
            default_idx = None

        if chosen_idx is not None and default_idx is not None:
            denom = max(len(candidates) - 1, 1)
            distances.append(abs(chosen_idx - default_idx) / denom)
            continue

        chosen_num = _coerce_numeric(chosen)
        default_num = _coerce_numeric(default)
        numeric_candidates = [_coerce_numeric(v) for v in candidates]
        if (
            chosen_num is not None
            and default_num is not None
            and all(v is not None for v in numeric_candidates)
        ):
            spread = max(numeric_candidates) - min(numeric_candidates)
            distances.append(0.0 if spread == 0 else abs(chosen_num - default_num) / spread)
            continue

        distances.append(1.0 if chosen != default else 0.0)

    return float(np.mean(distances)) if distances else 0.0


# ---------------------------------------------------------------------------
# Walk-forward splits
# ---------------------------------------------------------------------------

def make_walk_forward_splits(
    start: str,
    end: str,
    train_months: int = 24,
    val_months: int = 6,
    step_months: int = 6,
) -> List[Tuple[str, str, str, str]]:
    """
    Generate rolling (train_start, train_end, val_start, val_end) tuples.

    Parameters
    ----------
    start, end : date strings
    train_months : size of training window in months
    val_months : size of validation window in months
    step_months : how far to roll forward each fold
    """
    splits = []
    current = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)

    while True:
        train_start = current
        train_end = train_start + pd.DateOffset(months=train_months) - pd.Timedelta(days=1)
        val_start = train_end + pd.Timedelta(days=1)
        val_end = val_start + pd.DateOffset(months=val_months) - pd.Timedelta(days=1)

        if val_end > end_ts:
            # Allow partial last fold if at least half the val window fits
            if val_start + pd.DateOffset(months=val_months // 2) <= end_ts:
                val_end = end_ts
            else:
                break

        splits.append((
            str(train_start.date()),
            str(train_end.date()),
            str(val_start.date()),
            str(val_end.date()),
        ))
        current += pd.DateOffset(months=step_months)

    return splits


# ---------------------------------------------------------------------------
# Strategy builders with config overrides
# ---------------------------------------------------------------------------

def _build_portfolio_strategy(
    price_data: Dict[str, pd.DataFrame],
    mode: str,
    profile_name: str = "daily",
    trend_cfg: Optional[Dict] = None,
    cs_cfg: Optional[Dict] = None,
    carry_cfg: Optional[Dict] = None,
    portfolio_weights: Optional[Dict[str, float]] = None,
) -> BaseStrategy:
    """Build combined portfolio strategy with optional config overrides."""
    return build_portfolio_strategy(
        price_data=price_data,
        mode=mode,
        profile_name=profile_name,
        trend_cfg=trend_cfg,
        cs_cfg=cs_cfg,
        carry_cfg=carry_cfg,
        portfolio_weights=portfolio_weights,
        label=f"{profile_name.title()} Portfolio (optimized)",
    )


def _build_single_strategy(
    name: str,
    cfg: Dict[str, Any],
    profile_name: str = "daily",
) -> BaseStrategy:
    return build_strategy(name, profile_name=profile_name, cfg=cfg)


# ---------------------------------------------------------------------------
# Single backtest run with metrics extraction
# ---------------------------------------------------------------------------

def _run_backtest_for_period(
    strategy: BaseStrategy,
    price_data: Dict[str, pd.DataFrame],
    mode: str,
    start: str,
    end: str,
    capital: float,
    profile_name: str = "daily",
    use_unified: bool = False,
) -> Dict[str, float]:
    """Run a backtest and return key metrics."""
    if use_unified:
        from backtest.runner import BacktestRunner
        runner = BacktestRunner(profile_name=profile_name, mode=mode, initial_capital=capital)
        results = runner.run(price_data, start=start, end=end)
    else:
        engine = BacktestEngine(
            strategy=strategy,
            price_data=price_data,
            mode=mode,
            initial_capital=capital,
            profile_name=profile_name,
        )
        results = engine.run(start=start, end=end)
    nav = results["nav_series"]

    if nav.empty or len(nav) < 10:
        return {
            "sharpe": 0.0,
            "calmar": 0.0,
            "annual_return": 0.0,
            "max_drawdown": 0.0,
            "sortino": 0.0,
            "nav": nav,
        }

    rets = daily_returns(nav)
    periods_per_year = infer_periods_per_year(nav.index, default=get_profile(profile_name)["periods_per_year"])
    return {
        "sharpe": sharpe_ratio(rets, periods_per_year=periods_per_year),
        "calmar": calmar_ratio(nav, periods_per_year=periods_per_year),
        "annual_return": annualised_return(rets, periods_per_year=periods_per_year),
        "max_drawdown": max_drawdown(nav),
        "sortino": sortino_ratio(rets, periods_per_year=periods_per_year),
        "nav": nav,
    }


# ---------------------------------------------------------------------------
# Core optimizer
# ---------------------------------------------------------------------------

OBJECTIVE_FUNCS = {
    "sharpe": lambda m: m["sharpe"],
    "calmar": lambda m: m["calmar"],
    "sortino": lambda m: m["sortino"],
    "risk_adjusted": lambda m: 0.5 * m["sharpe"] + 0.3 * m["calmar"] + 0.2 * m["sortino"],
}


class WalkForwardOptimizer:
    """
    Walk-forward parameter optimizer for strategy sleeves or portfolio weights.

    Parameters
    ----------
    target : str
        Which space to optimize: 'trend_momentum', 'cross_sectional',
        'funding_carry', or 'portfolio_weights'.
    objective : str
        Metric to maximize: 'sharpe', 'calmar', 'sortino', or 'risk_adjusted'.
    mode : str
        Trading mode.
    train_months, val_months, step_months : int
        Walk-forward window sizes.
    max_combos : int
        Max parameter combinations to try per fold (random sample if exceeded).
    capital : float
        Initial capital for each backtest.
    """

    def __init__(
        self,
        target: str = "portfolio_weights",
        objective: str = "sharpe",
        mode: str = TRADING_MODE,
        profile_name: str = "daily",
        train_months: int = 24,
        val_months: int = 6,
        step_months: int = 6,
        max_combos: int = 100,
        capital: float = INITIAL_CAPITAL,
        param_penalty_weight: float = 0.10,
        use_unified: bool = False,
    ):
        self.profile_name = profile_name
        self.profile = get_profile(profile_name)
        self.spaces = get_param_spaces(profile_name)
        if target not in self.spaces:
            raise ValueError(f"Unknown target '{target}'. Choose from: {list(self.spaces.keys())}")
        if objective not in OBJECTIVE_FUNCS:
            raise ValueError(f"Unknown objective '{objective}'. Choose from: {list(OBJECTIVE_FUNCS.keys())}")

        self.target = target
        self.space = get_param_space(target, profile_name)
        self.objective = objective
        self.obj_func = OBJECTIVE_FUNCS[objective]
        self.mode = mode
        self.train_months = train_months
        self.val_months = val_months
        self.step_months = step_months
        self.max_combos = max_combos
        self.capital = capital
        self.param_penalty_weight = param_penalty_weight
        self.use_unified = use_unified

    def _get_param_combos(self) -> List[Dict[str, Any]]:
        total = self.space.num_combinations
        if total <= self.max_combos:
            return self.space.grid()
        logger.info(
            "Sampling %d/%d combinations (full grid too large)", self.max_combos, total
        )
        return self.space.sample(self.max_combos)

    def _build_strategy(
        self,
        price_data: Dict[str, pd.DataFrame],
        overrides: Dict[str, Any],
    ) -> BaseStrategy:
        """Build a strategy with the given parameter overrides."""
        if self.target == "portfolio_weights":
            return _build_portfolio_strategy(
                price_data, self.mode, profile_name=self.profile_name, portfolio_weights=overrides,
            )
        elif self.target == "trend_momentum":
            cfg = self.space.make_config(overrides)
            return _build_portfolio_strategy(
                price_data, self.mode, profile_name=self.profile_name, trend_cfg=cfg,
            )
        elif self.target == "cross_sectional":
            cfg = self.space.make_config(overrides)
            return _build_portfolio_strategy(
                price_data, self.mode, profile_name=self.profile_name, cs_cfg=cfg,
            )
        elif self.target == "funding_carry":
            cfg = self.space.make_config(overrides)
            return _build_portfolio_strategy(
                price_data, self.mode, profile_name=self.profile_name, carry_cfg=cfg,
            )
        else:
            cfg = self.space.make_config(overrides)
            return _build_single_strategy(self.target, cfg, profile_name=self.profile_name)

    def _optimize_fold(
        self,
        fold_idx: int,
        price_data: Dict[str, pd.DataFrame],
        train_start: str,
        train_end: str,
        val_start: str,
        val_end: str,
    ) -> FoldResult:
        combos = self._get_param_combos()
        logger.info(
            "Fold %d: train=%s→%s, val=%s→%s, testing %d combos",
            fold_idx, train_start, train_end, val_start, val_end, len(combos),
        )

        best_score = -np.inf
        best_params = combos[0]
        best_train_metrics = None
        best_train_objective = -np.inf
        best_penalty = 0.0
        raw_scores: List[float] = []

        for i, params in enumerate(combos):
            strategy = self._build_strategy(price_data, params)
            metrics = _run_backtest_for_period(
                strategy,
                price_data,
                self.mode,
                train_start,
                train_end,
                self.capital,
                profile_name=self.profile_name,
                use_unified=self.use_unified,
            )
            objective_value = self.obj_func(metrics)
            penalty = self.param_penalty_weight * _param_distance_from_defaults(self.space, params)
            score = objective_value - penalty
            raw_scores.append(objective_value)

            if score > best_score:
                best_score = score
                best_params = params
                best_train_metrics = metrics
                best_train_objective = objective_value
                best_penalty = penalty

            if (i + 1) % 20 == 0:
                logger.info("  ... tested %d/%d combos (best %s=%.4f)", i + 1, len(combos), self.objective, best_score)

        # Validate best params on holdout
        val_strategy = self._build_strategy(price_data, best_params)
        val_metrics = _run_backtest_for_period(
            val_strategy,
            price_data,
            self.mode,
            val_start,
            val_end,
            self.capital,
            profile_name=self.profile_name,
            use_unified=self.use_unified,
        )
        val_objective = self.obj_func(val_metrics)
        train_score_gap = best_train_objective - float(np.median(raw_scores)) if raw_scores else 0.0

        return FoldResult(
            fold_idx=fold_idx,
            train_start=train_start,
            train_end=train_end,
            val_start=val_start,
            val_end=val_end,
            best_params=best_params,
            train_sharpe=best_train_metrics["sharpe"] if best_train_metrics else 0.0,
            train_calmar=best_train_metrics["calmar"] if best_train_metrics else 0.0,
            val_sharpe=val_metrics["sharpe"],
            val_calmar=val_metrics["calmar"],
            val_annual_return=val_metrics["annual_return"],
            val_max_drawdown=val_metrics["max_drawdown"],
            train_objective=best_train_objective,
            val_objective=val_objective,
            param_penalty=best_penalty,
            candidate_count=len(combos),
            train_score_gap=train_score_gap,
            val_nav=val_metrics["nav"],
        )

    def run(
        self,
        price_data: Dict[str, pd.DataFrame],
        start: str,
        end: str,
    ) -> OptimizationResult:
        """
        Run walk-forward optimization across all folds.

        Parameters
        ----------
        price_data : fetched price data dict
        start, end : overall date range
        """
        t0 = time.time()
        splits = make_walk_forward_splits(
            start, end, self.train_months, self.val_months, self.step_months,
        )
        if not splits:
            raise ValueError(
                f"No valid WFO splits for {start}→{end} with "
                f"train={self.train_months}m, val={self.val_months}m"
            )

        logger.info(
            "Walk-forward optimization: %d folds, target=%s, objective=%s",
            len(splits), self.target, self.objective,
        )

        folds = []
        for idx, (ts, te, vs, ve) in enumerate(splits):
            fold = self._optimize_fold(idx, price_data, ts, te, vs, ve)
            folds.append(fold)
            logger.info(
                "Fold %d done: train_%s=%.4f, val_%s=%.4f, penalty=%.4f, params=%s",
                idx, self.objective, fold.train_objective,
                self.objective, fold.val_objective,
                fold.param_penalty, fold.best_params,
            )

        # Stitch together out-of-sample NAV series
        oos_navs = [f.val_nav for f in folds if f.val_nav is not None and not f.val_nav.empty]
        oos_nav = None
        if oos_navs:
            # Chain: normalize each fold's NAV to start where the previous ended
            chained = [oos_navs[0]]
            for nav in oos_navs[1:]:
                scale = chained[-1].iloc[-1] / nav.iloc[0]
                chained.append(nav * scale)
            oos_nav = pd.concat(chained)
            oos_nav = oos_nav[~oos_nav.index.duplicated(keep="last")]

        elapsed = time.time() - t0
        return OptimizationResult(
            target=self.target,
            objective=self.objective,
            folds=folds,
            oos_nav=oos_nav,
            elapsed_seconds=elapsed,
        )


# ---------------------------------------------------------------------------
# Pretty printing
# ---------------------------------------------------------------------------

def print_optimization_result(result: OptimizationResult) -> None:
    """Print a formatted summary of optimization results."""
    print(f"\n{'='*65}")
    print(f"  WALK-FORWARD OPTIMIZATION RESULTS")
    print(f"  Target: {result.target}  |  Objective: {result.objective}")
    print(f"  Folds: {len(result.folds)}  |  Time: {result.elapsed_seconds:.1f}s")
    print(f"{'='*65}")

    # Per-fold table
    print(f"\n{'Fold':>4} {'Train Period':>24} {'Val Period':>24} {'Train Sharpe':>13} {'Val Sharpe':>11} {'Val Calmar':>11}")
    print("-" * 95)
    for f in result.folds:
        print(
            f"{f.fold_idx:>4} {f.train_start+'→'+f.train_end:>24} "
            f"{f.val_start+'→'+f.val_end:>24} "
            f"{f.train_sharpe:>13.4f} {f.val_sharpe:>11.4f} {f.val_calmar:>11.4f}"
        )

    print(f"\n{'─'*65}")
    obj_label = result.objective.title()
    print(f"  Avg Train {obj_label}:{' '*(14-len(obj_label))}{result.avg_train_objective:>10.4f}")
    print(f"  Avg Val {obj_label}:{' '*(16-len(obj_label))}{result.avg_val_objective:>10.4f}")
    print(f"  Sharpe Degradation:    {result.sharpe_degradation:>10.1%}")
    print(f"  Param Stability:       {result.param_stability:>10.1%}")
    print(f"  Avg Score Gap:         {result.avg_train_score_gap:>10.4f}")
    print(f"  Avg Param Penalty:     {result.avg_param_penalty:>10.4f}")

    # OOS equity curve summary
    if result.oos_nav is not None and len(result.oos_nav) > 10:
        rets = daily_returns(result.oos_nav)
        periods_per_year = infer_periods_per_year(result.oos_nav.index)
        print(f"\n{'─'*65}")
        print(f"  Stitched OOS Performance:")
        print(f"    Sharpe:              {sharpe_ratio(rets, periods_per_year=periods_per_year):>10.4f}")
        print(f"    Annual Return:       {annualised_return(rets, periods_per_year=periods_per_year)*100:>10.2f}%")
        print(f"    Max Drawdown:        {max_drawdown(result.oos_nav)*100:>10.2f}%")
        print(f"    Calmar:              {calmar_ratio(result.oos_nav):>10.4f}")

    # Best params per fold
    print(f"\n{'─'*65}")
    print("  Best Parameters per Fold:")
    for f in result.folds:
        print(f"    Fold {f.fold_idx}: {f.best_params}")

    # Overfit warnings
    warnings = result.overfit_warnings()
    if warnings:
        print(f"\n{'!'*65}")
        print("  OVERFIT DIAGNOSTICS:")
        for w in warnings:
            print(f"    ⚠ {w}")
        print(f"{'!'*65}")
    else:
        print(f"\n  ✓ No major overfit warnings detected.")

    print(f"{'='*65}\n")
