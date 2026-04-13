"""
core/portfolio_construction.py
================================
PortfolioConstructionModel: converts Insights into final target weights.

Maps to LEAN's PortfolioConstructionModel. Takes the raw insights from
the AlphaModel and applies filtering, concentration limits, and leverage
constraints to produce the final Dict[str, float] passed to RiskModel.

Concrete implementations:
  SignalFilteredPortfolioModel — threshold filter + concentration cap +
                                  PortfolioManager leverage/exposure constraints.
                                  Replicates the filter pipeline previously
                                  inlined in TradingEngine._generate_target_weights().
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List

import pandas as pd

from core.insights import Insight

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class PortfolioConstructionModel(ABC):
    """
    Convert a list of Insights into final target weights.

    create_targets() receives raw insights from the AlphaModel, applies
    whatever filtering/sizing logic is appropriate, and returns a
    Dict[symbol → target_weight] that is forwarded to the RiskModel.
    """

    @abstractmethod
    def create_targets(
        self,
        insights: List[Insight],
        profile: Dict[str, Any],
        mode: str,
    ) -> Dict[str, float]:
        """
        Args:
            insights:  raw insights from AlphaModel
            profile:   profile config dict
            mode:      "spot" | "futures" | "margin"

        Returns:
            Dict[symbol → target_weight], already normalised and constrained.
        """
        ...


# ---------------------------------------------------------------------------
# SignalFilteredPortfolioModel
# ---------------------------------------------------------------------------

class SignalFilteredPortfolioModel(PortfolioConstructionModel):
    """
    Three-stage filter pipeline, then PortfolioManager constraints:

      1. Signal threshold  — drop positions below min_weight_threshold
      2. Concentration cap — keep only top max_positions by abs weight
      3. Leverage/exposure — PortfolioManager.apply_constraints()

    The filter decisions are recorded in the signal_meta dict that is
    carried on the Insight objects and aggregated by TradingAlgorithm
    for logging.
    """

    def create_targets(
        self,
        insights: List[Insight],
        profile: Dict[str, Any],
        mode: str,
    ) -> Dict[str, float]:
        from portfolio.portfolio_manager import PortfolioManager

        portfolio_cfg = profile.get("portfolio", {})
        min_weight = float(portfolio_cfg.get("min_weight_threshold", 0.03))
        max_positions = int(portfolio_cfg.get("max_positions", 10))

        # Seed from insight weights
        weights = pd.Series({i.symbol: i.weight for i in insights})

        # 1. Signal threshold filter
        n_before = (weights.abs() > 1e-8).sum()
        weights[weights.abs() < min_weight] = 0.0
        n_after = (weights.abs() > 1e-8).sum()
        if n_before != n_after:
            old_gross = pd.Series({i.symbol: i.weight for i in insights}).abs().sum()
            new_gross = weights.abs().sum()
            if new_gross > 0:
                weights *= old_gross / new_gross
            logger.info(
                "Signal threshold (%.1f%%): %d → %d positions (dropped %d)",
                min_weight * 100, n_before, n_after, n_before - n_after,
            )

        # 2. Concentration filter
        weights = self._apply_concentration_filter(weights, max_positions)

        # 3. Portfolio constraints (leverage, net exposure, per-position caps)
        pm = PortfolioManager(
            max_gross_leverage=float(portfolio_cfg.get("max_gross_leverage", 1.5)),
            max_net_exposure=float(portfolio_cfg.get("max_net_exposure", 0.5)),
            max_position_pct=float(portfolio_cfg.get("max_position_pct", 0.25)),
        )
        constrained = pm.apply_constraints(weights, mode=mode)

        return {k: round(float(v), 6) for k, v in constrained.items() if abs(v) > 1e-8}

    @staticmethod
    def _apply_concentration_filter(weights: pd.Series, max_positions: int) -> pd.Series:
        nonzero = weights[weights.abs() > 1e-8]
        if len(nonzero) <= max_positions:
            return weights

        top_n = nonzero.abs().nlargest(max_positions).index
        filtered = weights.copy()
        filtered[~filtered.index.isin(top_n)] = 0.0

        old_gross = weights.abs().sum()
        new_gross = filtered.abs().sum()
        if new_gross > 0 and old_gross > 0:
            filtered *= old_gross / new_gross

        n_dropped = len(nonzero) - max_positions
        logger.info(
            "Concentration filter: %d → %d positions (dropped %d smallest)",
            len(nonzero), max_positions, n_dropped,
        )
        return filtered
