"""
Helpers for building profile-aware strategies and combined portfolios.
"""

from __future__ import annotations

from typing import Dict, Optional

import pandas as pd

from config.profiles import get_profile
from signals.combiner import SignalCombiner
from strategies.base import BaseStrategy, StrategyOutput
from strategies.cross_sectional_momentum import CrossSectionalMomentumStrategy
from strategies.funding_carry import FundingCarryStrategy
from strategies.trend_momentum import TrendMomentumStrategy


STRATEGY_KEYS = {
    "trend": "trend",
    "trend_momentum": "trend",
    "cross_sectional": "cross_sectional",
    "carry": "carry",
    "funding_carry": "carry",
}


class CombinedPortfolioStrategy(BaseStrategy):
    name = "Portfolio"
    preferred_mode = "futures"
    sleeve_key = "portfolio"

    def __init__(self, output: StrategyOutput, label: str = "Portfolio"):
        self.output = output
        self.name = label

    def generate(self, price_data, mode: str = "spot") -> StrategyOutput:
        return self.output


def build_strategy(
    name: str,
    profile_name: str = "daily",
    cfg: Optional[Dict] = None,
) -> BaseStrategy:
    profile = get_profile(profile_name)
    sleeve_key = STRATEGY_KEYS[name]
    base_cfg = profile["strategies"][sleeve_key]
    merged_cfg = {**base_cfg, **(cfg or {})}

    if sleeve_key == "trend":
        return TrendMomentumStrategy(cfg=merged_cfg)
    if sleeve_key == "cross_sectional":
        return CrossSectionalMomentumStrategy(cfg=merged_cfg)
    if sleeve_key == "carry":
        return FundingCarryStrategy(cfg=merged_cfg)
    raise ValueError(f"Unknown strategy: {name}")


def build_portfolio_strategy(
    price_data: Dict[str, pd.DataFrame],
    mode: str,
    profile_name: str = "daily",
    trend_cfg: Optional[Dict] = None,
    cs_cfg: Optional[Dict] = None,
    carry_cfg: Optional[Dict] = None,
    portfolio_weights: Optional[Dict[str, float]] = None,
    label: Optional[str] = None,
) -> CombinedPortfolioStrategy:
    profile = get_profile(profile_name)
    weights = dict(profile["portfolio_weights"])
    if portfolio_weights:
        weights.update(portfolio_weights)

    # Per-sleeve config overrides (legacy positional args)
    _sleeve_cfg_overrides = {
        "trend": trend_cfg,
        "cross_sectional": cs_cfg,
        "carry": carry_cfg,
    }

    # Build only sleeves that have non-zero weight
    sleeves = {}
    for sleeve_key, w in weights.items():
        if w <= 0:
            continue
        try:
            cfg_override = _sleeve_cfg_overrides.get(sleeve_key)
            strat = build_strategy(sleeve_key, profile_name=profile_name, cfg=cfg_override)
            sleeves[sleeve_key] = strat.generate(price_data, mode=mode)
        except ValueError:
            pass
    output = SignalCombiner(sleeve_weights=weights).combine(sleeves, mode=mode)

    # Apply portfolio-level leverage multiplier
    leverage = float(profile.get("portfolio", {}).get("portfolio_leverage", 1.0))
    if leverage != 1.0:
        output = StrategyOutput(
            signal_direction=output.signal_direction,
            signal_strength=output.signal_strength,
            target_weights=output.target_weights * leverage,
            confidence=output.confidence,
            metadata=output.metadata,
        )

    strategy_label = label or f"{profile['name'].title()} Portfolio"
    return CombinedPortfolioStrategy(output=output, label=strategy_label)
