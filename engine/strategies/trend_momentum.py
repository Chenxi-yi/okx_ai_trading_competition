"""
Time-series trend and momentum sleeve.
"""

from __future__ import annotations

from typing import Dict, Optional

import numpy as np
import pandas as pd

from config.settings import BACKTEST_GUARDS, TREND_MOMENTUM
from strategies.base import BaseStrategy, StrategyOutput


class TrendMomentumStrategy(BaseStrategy):
    name = "Trend Momentum"
    preferred_mode = "futures"
    sleeve_key = "trend"

    def __init__(self, cfg: Optional[Dict] = None):
        self.cfg = {**TREND_MOMENTUM, **(cfg or {})}

    def generate(self, price_data: Dict[str, pd.DataFrame], mode: str = "spot") -> StrategyOutput:
        closes = self.build_close_matrix(
            price_data,
            max_ffill_days=int(self.cfg.get("max_ffill_bars", BACKTEST_GUARDS["max_ffill_days"])),
        )
        returns = self.build_return_matrix(closes)

        dmac_frames = []
        for fast in self.cfg["fast_windows"]:
            fast_ma = closes.rolling(fast, min_periods=fast).mean()
            for slow in self.cfg["slow_windows"]:
                if fast >= slow:
                    continue
                slow_ma = closes.rolling(slow, min_periods=slow).mean()
                sig = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
                sig[fast_ma > slow_ma * (1 + self.cfg["band"])] = 1.0
                sig[fast_ma < slow_ma * (1 - self.cfg["band"])] = -1.0
                dmac_frames.append(sig)
        dmac = sum(dmac_frames) / max(len(dmac_frames), 1)

        breakout_frames = []
        for window in self.cfg["breakout_windows"]:
            rolling_high = closes.shift(1).rolling(window, min_periods=window).max()
            rolling_low = closes.shift(1).rolling(window, min_periods=window).min()
            sig = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
            sig[closes > rolling_high] = 1.0
            sig[closes < rolling_low] = -1.0
            breakout_frames.append(sig)
        breakout = sum(breakout_frames) / max(len(breakout_frames), 1)

        momentum_frames = []
        for window in self.cfg["momentum_windows"]:
            momentum_frames.append(np.sign(closes.pct_change(window)).fillna(0.0))
        momentum = sum(momentum_frames) / max(len(momentum_frames), 1)

        raw_score = (dmac + breakout + momentum) / 3.0
        inv_vol = self.inverse_vol_scale(
            returns,
            self.cfg["vol_lookback"],
            cap=2.0,
            periods_per_year=int(self.cfg.get("periods_per_year", 365)),
        )
        weights = np.sign(raw_score) * inv_vol
        target_weights = self.normalize_cross_section(weights, gross_target=1.0)
        confidence = raw_score.abs().clip(upper=1.0).fillna(0.0)

        output = self.make_output(
            target_weights=target_weights,
            confidence=confidence,
            metadata={"components": ["dmac", "breakout", "tsmom"], "config": self.cfg},
        )
        return output.clip_for_mode(mode)
