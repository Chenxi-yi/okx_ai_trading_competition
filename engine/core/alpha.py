"""
core/alpha.py
=============
AlphaModel: generates trading insights from market data.

Maps to LEAN's AlphaModel. Receives price data, emits a list of
Insights (direction + weight per symbol). The PortfolioConstructionModel
then converts insights into final target weights.

Concrete implementations:
  CombinedAlphaModel — wraps the existing three-sleeve combined portfolio
                        strategy (trend + cross-sectional + carry).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from core.insights import Insight

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class AlphaModel(ABC):
    """
    Generate Insights from price data.

    generate() is called once per rebalance cycle with the full
    market data for the universe. It returns a list of Insights
    (one per symbol with a non-zero view) plus a signal_meta dict
    that is forwarded to the logger for debugging.
    """

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def generate(
        self,
        price_data: Dict[str, pd.DataFrame],
        symbols: List[str],
        mode: str,
        profile: Dict[str, Any],
    ) -> Tuple[List[Insight], Dict[str, Any]]:
        """
        Args:
            price_data: {symbol: OHLCV DataFrame}
            symbols:    universe symbol list
            mode:       "spot" | "futures" | "margin"
            profile:    profile config dict (timeframe, weights, …)

        Returns:
            (insights, signal_meta)
            insights:    list of Insight objects, one per symbol with a view
            signal_meta: arbitrary dict for logging (sleeve breakdown, filters, etc.)
        """
        ...


# ---------------------------------------------------------------------------
# CombinedAlphaModel  (wraps the existing three-sleeve strategy)
# ---------------------------------------------------------------------------

class CombinedAlphaModel(AlphaModel):
    """
    Generates insights from the combined portfolio strategy:
      trend (60%) + cross-sectional (25%) + carry (15%)  [hourly weights]
      trend (50%) + cross-sectional (35%) + carry (15%)  [daily weights]

    Replicates the logic previously inlined in
    TradingEngine._generate_target_weights() and
    TradingEngine._generate_with_sleeve_decomposition().
    """

    @property
    def name(self) -> str:
        return "combined_portfolio"

    def generate(
        self,
        price_data: Dict[str, pd.DataFrame],
        symbols: List[str],
        mode: str,
        profile: Dict[str, Any],
    ) -> Tuple[List[Insight], Dict[str, Any]]:
        from strategies.factory import build_portfolio_strategy, build_strategy

        profile_name = profile.get("name", "daily")
        signal_meta: Dict[str, Any] = {
            "strategy_id": self.name,
            "profile": profile_name,
        }

        # ---- Sleeve decomposition for logging ----
        sleeve_data = self._decompose_sleeves(price_data, profile_name, mode, profile)
        signal_meta["sleeves"] = sleeve_data

        # ---- Combined portfolio weights ----
        # Pass portfolio_weights from the (already-overridden) profile so that
        # competition strategy overrides (e.g. {"mean_reversion": 1.0}) propagate.
        strategy = build_portfolio_strategy(
            price_data=price_data,
            mode=mode,
            profile_name=profile_name,
            portfolio_weights=profile.get("portfolio_weights"),
        )
        output = strategy.generate(price_data, mode=mode)

        # Use iloc[-2] for daily (trade on yesterday's signal), iloc[-1] for hourly
        row = output.target_weights.iloc[-2] if len(output.target_weights) >= 2 else output.target_weights.iloc[-1]
        confidence_row = output.confidence.iloc[-2] if len(output.confidence) >= 2 else output.confidence.iloc[-1]

        raw_weights = row.to_dict()
        signal_meta["raw_combined_weights"] = {k: round(v, 6) for k, v in raw_weights.items() if abs(v) > 1e-8}
        signal_meta["n_raw_signals"] = sum(1 for v in raw_weights.values() if abs(v) > 1e-8)

        # ---- Build Insights ----
        insights = []
        for sym, w in raw_weights.items():
            if abs(w) < 1e-8:
                continue
            conf = float(confidence_row.get(sym, 0.5)) if hasattr(confidence_row, "get") else 0.5
            sw = {
                sleeve: data.get("all_weights", {}).get(sym, 0.0)
                for sleeve, data in sleeve_data.items()
                if not data.get("error")
            }
            insights.append(Insight(
                symbol=sym,
                direction=1 if w > 0 else -1,
                weight=float(w),
                confidence=conf,
                source=self.name,
                sleeve_weights={k: v for k, v in sw.items() if abs(v) > 1e-8},
            ))

        return insights, signal_meta

    # ------------------------------------------------------------------

    def _decompose_sleeves(
        self,
        price_data: Dict[str, pd.DataFrame],
        profile_name: str,
        mode: str,
        resolved_profile: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Per-sleeve weight breakdown for logging/debugging."""
        from config.profiles import get_profile
        from strategies.factory import build_strategy

        profile = resolved_profile if resolved_profile is not None else get_profile(profile_name)
        sleeve_data: Dict[str, Any] = {}
        sleeve_names = list(profile.get("portfolio_weights", {}).keys())

        for sleeve_name in sleeve_names:
            try:
                strat = build_strategy(sleeve_name, profile_name=profile_name)
                out = strat.generate(price_data, mode=mode)
                row = out.target_weights.iloc[-2] if len(out.target_weights) >= 2 else out.target_weights.iloc[-1]
                sleeve_weights = {k: round(float(v), 6) for k, v in row.to_dict().items() if abs(v) > 1e-6}
                sorted_w = sorted(sleeve_weights.items(), key=lambda x: abs(x[1]), reverse=True)
                sleeve_data[sleeve_name] = {
                    "portfolio_weight": profile["portfolio_weights"].get(sleeve_name, 0),
                    "n_signals": len(sleeve_weights),
                    "n_long": sum(1 for v in sleeve_weights.values() if v > 0),
                    "n_short": sum(1 for v in sleeve_weights.values() if v < 0),
                    "top_signals": dict(sorted_w[:10]),
                    "all_weights": sleeve_weights,
                }
            except Exception as e:
                sleeve_data[sleeve_name] = {"error": str(e)}

        return sleeve_data
