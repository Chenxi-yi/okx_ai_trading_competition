"""
Funding-aware carry sleeve.
"""

from __future__ import annotations

from typing import Dict, Optional

import numpy as np
import pandas as pd

from config.settings import BACKTEST_GUARDS, FUNDING_CARRY
from strategies.base import BaseStrategy, StrategyOutput


class FundingCarryStrategy(BaseStrategy):
    name = "Funding Carry"
    preferred_mode = "futures"
    sleeve_key = "carry"

    def __init__(self, cfg: Optional[Dict] = None):
        self.cfg = {**FUNDING_CARRY, **(cfg or {})}

    def generate(self, price_data: Dict[str, pd.DataFrame], mode: str = "spot") -> StrategyOutput:
        closes = self.build_close_matrix(
            price_data,
            max_ffill_days=int(self.cfg.get("max_ffill_bars", BACKTEST_GUARDS["max_ffill_days"])),
        )
        index = closes.index
        columns = closes.columns
        funding_data = {sym: df.get("funding_rate", pd.Series(0.0, index=df.index)) for sym, df in price_data.items()}
        funding = self.build_funding_matrix(funding_data, index=index, columns=columns)

        smoothed = pd.DataFrame(0.0, index=index, columns=columns)
        for window in self.cfg["funding_lookbacks"]:
            smoothed = smoothed.add(funding.rolling(window, min_periods=max(1, window // 2)).mean(), fill_value=0.0)
        smoothed = smoothed / max(len(self.cfg["funding_lookbacks"]), 1)

        trend = closes.pct_change(self.cfg["trend_veto_lookback"])
        raw = pd.DataFrame(0.0, index=index, columns=columns)

        long_mask = (smoothed < -self.cfg["action_threshold"]) & (trend >= 0)
        short_mask = (smoothed > self.cfg["action_threshold"]) & (trend <= 0)

        raw[long_mask] = (-smoothed[long_mask]).clip(upper=self.cfg["max_abs_funding"])
        if mode != "spot":
            raw[short_mask] = (-smoothed[short_mask]).clip(lower=-self.cfg["max_abs_funding"])

        target = self.normalize_cross_section(raw.fillna(0.0), gross_target=1.0)
        confidence = (smoothed.abs() / max(self.cfg["action_threshold"], 1e-8)).clip(upper=1.0).fillna(0.0)

        output = self.make_output(
            target_weights=target,
            confidence=confidence,
            metadata={"components": ["funding_carry"], "config": self.cfg},
        )
        return output.clip_for_mode(mode)
