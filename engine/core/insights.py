"""
core/insights.py
================
Insight: the unit of information that an AlphaModel produces.

Maps to LEAN's Insight but simplified for our use case —
direction + weight per symbol, with optional per-sleeve breakdown
for logging.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass
class Insight:
    """
    A single alpha signal for one symbol.

    Attributes:
        symbol:         ccxt symbol, e.g. "BTC/USDT"
        direction:      +1 long, -1 short, 0 flat
        weight:         raw portfolio weight from the alpha model (pre-construction)
        confidence:     0–1 confidence score from the strategy
        source:         name of the AlphaModel that generated this
        sleeve_weights: per-sleeve weight breakdown {"trend": 0.12, "carry": -0.05, ...}
        metadata:       free-form extra data for debugging / logging
    """

    symbol: str
    direction: int
    weight: float
    confidence: float
    source: str
    sleeve_weights: Dict[str, float] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_long(self) -> bool:
        return self.direction > 0

    @property
    def is_short(self) -> bool:
        return self.direction < 0

    @property
    def is_flat(self) -> bool:
        return self.direction == 0

    def __repr__(self) -> str:
        side = "LONG" if self.is_long else ("SHORT" if self.is_short else "FLAT")
        return f"Insight({self.symbol} {side} w={self.weight:+.4f} conf={self.confidence:.2f} src={self.source})"
