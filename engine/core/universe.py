"""
core/universe.py
================
UniverseSelectionModel: decides which symbols are tradable.

Maps to LEAN's UniverseSelectionModel. Called once per rebalance
to return the symbol list that the data layer fetches and the
AlphaModel receives.

Concrete implementations:
  DynamicOKXUniverse  — wraps get_symbols(dynamic=True): fetches all
                         active USDT-M swaps from OKX filtered by volume.
  StaticUniverse      — fixed list, useful for backtesting or testing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class UniverseSelectionModel(ABC):
    """Return the tradable symbol list for a given trading mode."""

    @abstractmethod
    def select(self, mode: str) -> List[str]: ...


# ---------------------------------------------------------------------------
# Concrete implementations
# ---------------------------------------------------------------------------

class DynamicOKXUniverse(UniverseSelectionModel):
    """
    Fetches all active USDT-M perpetual swaps from OKX filtered by
    24h volume. Backed by settings.get_symbols(dynamic=True) which
    caches the result after the first call per process.
    """

    def select(self, mode: str) -> List[str]:
        from config.settings import get_symbols
        return get_symbols(mode, dynamic=True)


# Backwards-compatible alias
DynamicBinanceUniverse = DynamicOKXUniverse


class StaticUniverse(UniverseSelectionModel):
    """Fixed symbol list — useful for backtesting or narrow strategies."""

    def __init__(self, symbols: List[str]):
        self._symbols = list(symbols)

    def select(self, mode: str) -> List[str]:
        return list(self._symbols)
