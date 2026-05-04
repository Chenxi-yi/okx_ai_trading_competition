"""Validation checks for feature/label research panels."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Dict, List, Optional

import numpy as np
import pandas as pd


@dataclass
class ValidationReport:
    n_rows: int
    n_symbols: int
    n_features: int
    n_labels: int
    duplicate_index_rows: int
    feature_nan_pct: float
    label_nan_pct: float
    feature_inf_count: int
    label_inf_count: int
    feature_nan_by_column: Dict[str, float]
    excessive_nan_features: Dict[str, float]
    timestamp_gap_count: int
    symbols_with_timestamp_gaps: List[str]
    all_nan_features: Dict[str, bool]
    unregistered_features: Dict[str, bool]
    feature_registry_coverage: float
    status: str

    def to_dict(self) -> Dict:
        return asdict(self)


def validate_feature_label_panel(
    features: pd.DataFrame,
    labels: pd.DataFrame,
    feature_registry: Optional[Dict] = None,
    expected_frequency: Optional[str] = None,
    max_feature_nan_pct: float = 0.25,
) -> ValidationReport:
    common = features.index.intersection(labels.index)
    f = features.loc[common] if len(common) else features.iloc[0:0]
    l = labels.loc[common] if len(common) else labels.iloc[0:0]
    duplicate_rows = int(features.index.duplicated().sum() + labels.index.duplicated().sum())
    f_values = f.select_dtypes(include=["number"])
    l_values = l.select_dtypes(include=["number"])
    feature_cells = max(int(f_values.size), 1)
    label_cells = max(int(l_values.size), 1)
    f_inf = int(np.isinf(f_values.to_numpy(dtype=float, na_value=np.nan)).sum()) if not f_values.empty else 0
    l_inf = int(np.isinf(l_values.to_numpy(dtype=float, na_value=np.nan)).sum()) if not l_values.empty else 0
    all_nan = {col: bool(f[col].isna().all()) for col in f.columns}
    nan_by_col = {col: round(float(f_values[col].isna().mean()), 6) for col in f_values.columns}
    excessive_nan = {col: pct for col, pct in nan_by_col.items() if pct > max_feature_nan_pct}
    gap_count, symbols_with_gaps = _timestamp_gap_report(features, expected_frequency)
    registry_keys = set(feature_registry or {})
    unregistered = {col: True for col in f.columns if feature_registry is not None and col not in registry_keys}
    coverage = 1.0
    if feature_registry is not None and features.shape[1]:
        coverage = (features.shape[1] - len(unregistered)) / features.shape[1]

    status = "ok"
    if not len(common):
        status = "failed:no_common_index"
    elif duplicate_rows:
        status = "failed:duplicate_index"
    elif f_inf or l_inf:
        status = "failed:infinite_values"
    elif unregistered:
        status = "failed:unregistered_features"
    elif gap_count:
        status = "failed:timestamp_gaps"
    elif excessive_nan:
        status = "warn:excessive_feature_nan"
    elif any(all_nan.values()):
        status = "warn:all_nan_features"

    symbols = common.get_level_values("symbol").nunique() if isinstance(common, pd.MultiIndex) and len(common) else 0
    return ValidationReport(
        n_rows=int(len(common)),
        n_symbols=int(symbols),
        n_features=int(features.shape[1]),
        n_labels=int(labels.shape[1]),
        duplicate_index_rows=duplicate_rows,
        feature_nan_pct=round(float(f_values.isna().sum().sum() / feature_cells), 6),
        label_nan_pct=round(float(l_values.isna().sum().sum() / label_cells), 6),
        feature_inf_count=f_inf,
        label_inf_count=l_inf,
        feature_nan_by_column=nan_by_col,
        excessive_nan_features=excessive_nan,
        timestamp_gap_count=gap_count,
        symbols_with_timestamp_gaps=symbols_with_gaps,
        all_nan_features=all_nan,
        unregistered_features=unregistered,
        feature_registry_coverage=round(float(coverage), 6),
        status=status,
    )


def _timestamp_gap_report(features: pd.DataFrame, expected_frequency: Optional[str]) -> tuple[int, List[str]]:
    if not expected_frequency or features.empty or not isinstance(features.index, pd.MultiIndex):
        return 0, []
    try:
        expected = pd.Timedelta(expected_frequency)
    except ValueError:
        return 0, []

    gap_count = 0
    symbols: List[str] = []
    for symbol, frame in features.groupby(level="symbol", sort=False):
        timestamps = frame.index.get_level_values("timestamp").sort_values()
        diffs = timestamps.to_series(index=timestamps).diff().dropna()
        gaps = int((diffs > expected * 1.5).sum())
        if gaps:
            gap_count += gaps
            symbols.append(str(symbol))
    return gap_count, sorted(symbols)
