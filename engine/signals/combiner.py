"""
Sleeve combiner for target-weight strategy outputs.
"""

from __future__ import annotations

from typing import Dict, Optional

import numpy as np
import pandas as pd

from strategies.base import StrategyOutput


class SignalCombiner:
    def __init__(self, sleeve_weights: Optional[Dict[str, float]] = None):
        self.sleeve_weights = sleeve_weights or {}

    def combine(self, outputs: Dict[str, StrategyOutput], mode: str = "spot") -> StrategyOutput:
        if not outputs:
            raise ValueError("outputs is empty")

        first = next(iter(outputs.values()))
        all_idx = first.target_weights.index
        all_cols = first.target_weights.columns
        for output in outputs.values():
            all_idx = all_idx.union(output.target_weights.index)
            all_cols = all_cols.union(output.target_weights.columns)

        combined_weights = pd.DataFrame(0.0, index=all_idx, columns=all_cols)
        combined_conf = pd.DataFrame(0.0, index=all_idx, columns=all_cols)
        meta = {"sleeves": {}}

        total_weight = 0.0
        for sleeve_name, output in outputs.items():
            sleeve_weight = float(self.sleeve_weights.get(sleeve_name, 1.0))
            total_weight += sleeve_weight
            weights = output.target_weights.reindex(index=all_idx, columns=all_cols, fill_value=0.0)
            conf = output.confidence.reindex(index=all_idx, columns=all_cols, fill_value=0.0)
            combined_weights += sleeve_weight * weights
            combined_conf += sleeve_weight * conf
            meta["sleeves"][sleeve_name] = output.metadata

        if total_weight > 0:
            combined_weights /= total_weight
            combined_conf /= total_weight

        output = StrategyOutput(
            signal_direction=combined_weights.apply(lambda x: x.apply(lambda v: 1 if v > 0 else (-1 if v < 0 else 0))),
            signal_strength=combined_weights.abs().clip(upper=1.0),
            target_weights=combined_weights,
            confidence=combined_conf.clip(upper=1.0),
            metadata=meta,
        )
        return output.clip_for_mode(mode)
