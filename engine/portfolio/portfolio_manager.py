"""
Portfolio sizing and constraint helpers for sleeve-based strategies.
"""

from __future__ import annotations

from typing import Dict

import pandas as pd

from config.settings import MAX_GROSS_LEVERAGE, MAX_NET_EXPOSURE, MAX_POSITION_PCT, PORTFOLIO_WEIGHTS


class PortfolioManager:
    def __init__(
        self,
        max_gross_leverage: float = MAX_GROSS_LEVERAGE,
        max_net_exposure: float = MAX_NET_EXPOSURE,
        max_position_pct: float = MAX_POSITION_PCT,
    ):
        self.max_gross_leverage = max_gross_leverage
        self.max_net_exposure = max_net_exposure
        self.max_position_pct = max_position_pct
        self.sleeve_weights = dict(PORTFOLIO_WEIGHTS)

    def apply_constraints(self, target_weights: pd.Series, mode: str) -> pd.Series:
        weights = target_weights.copy().fillna(0.0)
        if mode == "spot":
            weights = weights.clip(lower=0.0)

        weights = weights.clip(lower=-self.max_position_pct, upper=self.max_position_pct)

        gross = float(weights.abs().sum())
        if gross > self.max_gross_leverage and gross > 0:
            weights *= self.max_gross_leverage / gross

        net = float(weights.sum())
        if abs(net) > self.max_net_exposure and gross > 0:
            adjustment = self.max_net_exposure / abs(net)
            weights *= adjustment

        return weights

    def align_output(self, weights: pd.DataFrame, mode: str) -> pd.DataFrame:
        rows = []
        for _, row in weights.iterrows():
            rows.append(self.apply_constraints(row, mode))
        return pd.DataFrame(rows, index=weights.index, columns=weights.columns).fillna(0.0)
