"""
Base interfaces for phase-1 strategy sleeves.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict

import numpy as np
import pandas as pd


@dataclass
class StrategyOutput:
    signal_direction: pd.DataFrame
    signal_strength: pd.DataFrame
    target_weights: pd.DataFrame
    confidence: pd.DataFrame
    metadata: Dict[str, object] = field(default_factory=dict)

    def clip_for_mode(self, mode: str) -> "StrategyOutput":
        if mode == "spot":
            self.signal_direction = self.signal_direction.clip(lower=0)
            self.target_weights = self.target_weights.clip(lower=0)
        return self


class BaseStrategy(ABC):
    name = "BaseStrategy"
    preferred_mode = "futures"
    sleeve_key = "base"

    @abstractmethod
    def generate(self, price_data: Dict[str, pd.DataFrame], mode: str = "spot") -> StrategyOutput:
        ...

    def preferred_leverage(self, mode: str) -> float:
        return 1.0

    @staticmethod
    def build_close_matrix(price_data: Dict[str, pd.DataFrame], max_ffill_days: int = 1) -> pd.DataFrame:
        closes = {sym: df["close"] for sym, df in price_data.items()}
        return pd.DataFrame(closes).sort_index().ffill(limit=max_ffill_days)

    @staticmethod
    def build_volume_matrix(price_data: Dict[str, pd.DataFrame]) -> pd.DataFrame:
        volumes = {sym: df["volume"] for sym, df in price_data.items()}
        return pd.DataFrame(volumes).sort_index().fillna(0.0)

    @staticmethod
    def build_return_matrix(close_matrix: pd.DataFrame) -> pd.DataFrame:
        return close_matrix.pct_change()

    @staticmethod
    def build_funding_matrix(funding_data: Dict[str, pd.Series], index: pd.Index, columns: pd.Index) -> pd.DataFrame:
        matrix = pd.DataFrame(0.0, index=index, columns=columns)
        for sym, series in funding_data.items():
            if sym in matrix.columns:
                matrix.loc[:, sym] = series.reindex(index).fillna(0.0)
        return matrix

    @staticmethod
    def inverse_vol_scale(
        returns: pd.DataFrame,
        lookback: int,
        cap: float = 1.0,
        periods_per_year: int = 365,
    ) -> pd.DataFrame:
        vol = returns.rolling(lookback, min_periods=max(5, lookback // 2)).std() * np.sqrt(periods_per_year)
        weights = (1.0 / vol.replace(0, np.nan)).replace([np.inf, -np.inf], np.nan).fillna(0.0)
        return weights.clip(upper=cap)

    @staticmethod
    def normalize_cross_section(weights: pd.DataFrame, gross_target: float = 1.0) -> pd.DataFrame:
        gross = weights.abs().sum(axis=1).replace(0, np.nan)
        scaled = weights.div(gross, axis=0).fillna(0.0)
        return scaled * gross_target

    @staticmethod
    def make_output(
        target_weights: pd.DataFrame,
        confidence: pd.DataFrame | None = None,
        metadata: Dict[str, object] | None = None,
    ) -> StrategyOutput:
        directions = np.sign(target_weights).astype(int)
        strength = target_weights.abs().clip(upper=1.0)
        if confidence is None:
            confidence = strength.copy()
        return StrategyOutput(
            signal_direction=directions,
            signal_strength=strength,
            target_weights=target_weights.fillna(0.0),
            confidence=confidence.fillna(0.0),
            metadata=metadata or {},
        )
