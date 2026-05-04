"""Data quality checks used before research, paper, or live decisions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class DataQualityReport:
    status: str
    n_symbols: int
    duplicate_rows: int = 0
    non_monotonic_symbols: tuple[str, ...] = ()
    stale_symbols: tuple[str, ...] = ()
    missing_columns: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    nan_pct_by_symbol: Mapping[str, float] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def validate_ohlcv_data(
    price_data: Mapping[str, pd.DataFrame],
    required_columns: tuple[str, ...] = ("open", "high", "low", "close", "volume"),
    max_nan_pct: float = 0.10,
    max_staleness: pd.Timedelta | None = None,
    now: pd.Timestamp | None = None,
) -> DataQualityReport:
    duplicate_rows = 0
    non_monotonic: list[str] = []
    stale: list[str] = []
    missing: dict[str, tuple[str, ...]] = {}
    nan_pct: dict[str, float] = {}
    now = now or pd.Timestamp.utcnow()

    for symbol, df in price_data.items():
        if df is None or df.empty:
            missing[symbol] = required_columns
            continue
        idx = pd.DatetimeIndex(pd.to_datetime(df.index, utc=True))
        duplicate_rows += int(idx.duplicated().sum())
        if not idx.is_monotonic_increasing:
            non_monotonic.append(symbol)
        miss = tuple(col for col in required_columns if col not in df.columns)
        if miss:
            missing[symbol] = miss
        numeric = df[[c for c in required_columns if c in df.columns]].apply(pd.to_numeric, errors="coerce")
        cells = max(int(numeric.size), 1)
        nan_pct[symbol] = float(numeric.isna().sum().sum() / cells)
        if max_staleness is not None and len(idx):
            age = now - idx.max()
            if age > max_staleness:
                stale.append(symbol)

    status = "ok"
    if missing:
        status = "failed:missing_columns"
    elif duplicate_rows:
        status = "failed:duplicate_rows"
    elif non_monotonic:
        status = "failed:non_monotonic_index"
    elif stale:
        status = "failed:stale_data"
    elif any(v > max_nan_pct or np.isnan(v) for v in nan_pct.values()):
        status = "warn:excessive_nan"

    return DataQualityReport(
        status=status,
        n_symbols=len(price_data),
        duplicate_rows=duplicate_rows,
        non_monotonic_symbols=tuple(sorted(non_monotonic)),
        stale_symbols=tuple(sorted(stale)),
        missing_columns=missing,
        nan_pct_by_symbol=nan_pct,
    )
