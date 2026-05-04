"""Market snapshot providers for paper/live runners."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import pandas as pd

from contracts import InstrumentSpec, MarketState


@dataclass
class OHLCVMarketProvider:
    """Sequential MarketState provider from OHLCV panels.

    This is useful for paper-runtime smoke tests and for wiring dashboard/runtime
    code before live websocket providers are enabled.
    """

    price_data: Mapping[str, pd.DataFrame]
    instruments: Mapping[str, InstrumentSpec]
    freshness_sec: float = 0.0

    def __post_init__(self) -> None:
        normalized = {}
        for symbol, df in self.price_data.items():
            out = df.copy()
            out.index = pd.to_datetime(out.index, utc=True)
            normalized[symbol] = out.sort_index()
        self.price_data = normalized
        self.timestamps = _timestamps(self.price_data)
        self.cursor = 0

    def __call__(self) -> tuple[MarketState, dict[str, float]]:
        if len(self.timestamps) == 0:
            raise RuntimeError("OHLCVMarketProvider has no timestamps")
        ts = self.timestamps[min(self.cursor, len(self.timestamps) - 1)]
        self.cursor = min(self.cursor + 1, len(self.timestamps))
        ohlcv = {
            symbol: df.loc[:ts].copy()
            for symbol, df in self.price_data.items()
            if not df.loc[:ts].empty
        }
        marks = {
            symbol: float(pd.to_numeric(df["close"], errors="coerce").dropna().iloc[-1])
            for symbol, df in ohlcv.items()
            if not pd.to_numeric(df["close"], errors="coerce").dropna().empty
        }
        market = MarketState(
            timestamp=ts.to_pydatetime(),
            universe=tuple(ohlcv),
            ohlcv=ohlcv,
            instruments=self.instruments,
            freshness_sec={symbol: self.freshness_sec for symbol in ohlcv},
        )
        return market, marks


def _timestamps(price_data: Mapping[str, pd.DataFrame]) -> pd.DatetimeIndex:
    values: pd.DatetimeIndex | None = None
    for df in price_data.values():
        idx = pd.DatetimeIndex(pd.to_datetime(df.index, utc=True))
        values = idx if values is None else values.union(idx)
    return pd.DatetimeIndex([], tz="UTC") if values is None else values.sort_values()
