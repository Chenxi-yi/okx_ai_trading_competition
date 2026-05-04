"""Immutable market-state contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class OHLCVSnapshot:
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    timeframe: str
    funding_rate: float | None = None


@dataclass(frozen=True)
class OrderBookSnapshot:
    inst_id: str
    timestamp: datetime
    bids: tuple[tuple[float, float], ...] = ()
    asks: tuple[tuple[float, float], ...] = ()

    @property
    def best_bid(self) -> float | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0][0] if self.asks else None

    @property
    def mid(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def spread_bps(self) -> float | None:
        mid = self.mid
        if mid is None or mid <= 0:
            return None
        return (self.best_ask - self.best_bid) / mid * 10_000.0  # type: ignore[operator]


@dataclass(frozen=True)
class InstrumentSpec:
    inst_id: str
    symbol: str
    ct_val: float
    lot_sz: float
    min_sz: float
    tick_sz: float | None = None
    max_mkt_sz: float | None = None
    max_leverage: float | None = None
    active: bool = True
    source: str = "unknown"
    timestamp: datetime | None = None


@dataclass(frozen=True)
class MarketState:
    timestamp: datetime
    universe: tuple[str, ...]
    ohlcv: Mapping[str, Any] = field(default_factory=dict)
    orderbooks: Mapping[str, OrderBookSnapshot] = field(default_factory=dict)
    funding: Mapping[str, float] = field(default_factory=dict)
    open_interest: Mapping[str, float] = field(default_factory=dict)
    long_short_ratio: Mapping[str, float] = field(default_factory=dict)
    instruments: Mapping[str, InstrumentSpec] = field(default_factory=dict)
    features: Mapping[str, Any] = field(default_factory=dict)
    freshness_sec: Mapping[str, float] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def is_fresh(self, max_age_sec: float) -> bool:
        return all(age <= max_age_sec for age in self.freshness_sec.values())

    def missing_symbols(self, required: Sequence[str]) -> tuple[str, ...]:
        have = set(self.universe)
        return tuple(symbol for symbol in required if symbol not in have)
