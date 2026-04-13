"""
Cross-sectional crypto momentum sleeve.
"""

from __future__ import annotations

from typing import Dict, Optional

import pandas as pd

from config.settings import BACKTEST_GUARDS, CROSS_SECTIONAL_MOMENTUM
from strategies.base import BaseStrategy, StrategyOutput


class CrossSectionalMomentumStrategy(BaseStrategy):
    name = "Cross-Sectional Momentum"
    preferred_mode = "futures"
    sleeve_key = "cross_sectional"

    def __init__(self, cfg: Optional[Dict] = None):
        self.cfg = {**CROSS_SECTIONAL_MOMENTUM, **(cfg or {})}

    def generate(self, price_data: Dict[str, pd.DataFrame], mode: str = "spot") -> StrategyOutput:
        closes = self.build_close_matrix(
            price_data,
            max_ffill_days=int(self.cfg.get("max_ffill_bars", BACKTEST_GUARDS["max_ffill_days"])),
        )
        returns = self.build_return_matrix(closes)

        scores = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
        for lookback in self.cfg["lookbacks"]:
            scores = scores.add(closes.pct_change(lookback).rank(axis=1, pct=True), fill_value=0.0)
        scores = scores / max(len(self.cfg["lookbacks"]), 1)

        inv_vol = self.inverse_vol_scale(
            returns,
            self.cfg["vol_lookback"],
            cap=2.0,
            periods_per_year=int(self.cfg.get("periods_per_year", 365)),
        )
        target = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
        confidence = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)

        for date, row in scores.iterrows():
            valid = row.dropna()
            if len(valid) < self.cfg["require_min_universe"]:
                continue
            ranked = valid.sort_values(ascending=False)
            longs = ranked.index[: self.cfg["top_n"]]
            shorts = ranked.index[-self.cfg["bottom_n"] :]

            target.loc[date, longs] = inv_vol.loc[date, longs]
            if mode != "spot":
                target.loc[date, shorts] = -inv_vol.loc[date, shorts]

            midpoint = valid.median()
            confidence.loc[date, valid.index] = (valid - midpoint).abs()

        target = self.normalize_cross_section(target, gross_target=1.0)
        confidence = confidence.clip(upper=1.0).fillna(0.0)

        output = self.make_output(
            target_weights=target,
            confidence=confidence,
            metadata={"components": ["cross_sectional_momentum"], "config": self.cfg},
        )
        return output.clip_for_mode(mode)
