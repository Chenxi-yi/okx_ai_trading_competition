"""Purged walk-forward split utilities for feature research."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Dict, List

import pandas as pd


@dataclass(frozen=True)
class WalkForwardFold:
    fold: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    purge_bars: int
    train_rows: int
    test_rows: int

    def to_dict(self) -> Dict:
        return asdict(self)


def build_purged_walk_forward_folds(
    index: pd.Index,
    train_bars: int,
    test_bars: int,
    purge_bars: int,
    step_bars: int | None = None,
) -> List[WalkForwardFold]:
    """
    Build chronological train/test folds with a purge gap before each test.

    The splitter operates on unique timestamps. For MultiIndex panels, pass the
    full panel index; row counts are computed across all symbols.
    """
    if train_bars <= 0 or test_bars <= 0 or purge_bars < 0:
        raise ValueError("train_bars and test_bars must be positive; purge_bars must be non-negative")
    if step_bars is None:
        step_bars = test_bars
    if step_bars <= 0:
        raise ValueError("step_bars must be positive")

    timestamps = _unique_timestamps(index)
    folds: List[WalkForwardFold] = []
    start = 0
    fold = 0
    while True:
        train_start_i = start
        train_end_i = train_start_i + train_bars
        test_start_i = train_end_i + purge_bars
        test_end_i = test_start_i + test_bars
        if test_end_i > len(timestamps):
            break

        train_ts = timestamps[train_start_i:train_end_i]
        test_ts = timestamps[test_start_i:test_end_i]
        folds.append(
            WalkForwardFold(
                fold=fold,
                train_start=str(train_ts[0]),
                train_end=str(train_ts[-1]),
                test_start=str(test_ts[0]),
                test_end=str(test_ts[-1]),
                purge_bars=purge_bars,
                train_rows=_row_count(index, train_ts),
                test_rows=_row_count(index, test_ts),
            )
        )
        fold += 1
        start += step_bars
    return folds


def folds_to_dicts(folds: List[WalkForwardFold]) -> List[Dict]:
    return [fold.to_dict() for fold in folds]


def _unique_timestamps(index: pd.Index) -> pd.DatetimeIndex:
    if isinstance(index, pd.MultiIndex):
        level = "timestamp" if "timestamp" in index.names else 0
        values = index.get_level_values(level)
    else:
        values = index
    return pd.DatetimeIndex(pd.to_datetime(values, utc=True).unique()).sort_values()


def _row_count(index: pd.Index, timestamps: pd.DatetimeIndex) -> int:
    if isinstance(index, pd.MultiIndex):
        return int(index.get_level_values("timestamp").isin(timestamps).sum())
    return int(pd.DatetimeIndex(index).isin(timestamps).sum())
