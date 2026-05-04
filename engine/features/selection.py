"""Feature selection diagnostics."""

from __future__ import annotations

from typing import Iterable

import pandas as pd


def _corr_without_optional_deps(g: pd.DataFrame, method: str) -> float:
    if len(g) < 3:
        return float("nan")
    if method == "spearman":
        return g["x"].rank().corr(g["y"].rank(), method="pearson")
    return g["x"].corr(g["y"], method=method)


def compute_ic_summary(
    features: pd.DataFrame,
    labels: pd.DataFrame,
    label_col: str = "fwd_ret_6",
    min_obs: int = 50,
    methods: Iterable[str] = ("spearman", "pearson"),
) -> pd.DataFrame:
    """
    Compute per-feature information coefficient against one label.

    IC is computed cross-sectionally by timestamp, then summarized across time.
    """
    if features.empty or labels.empty or label_col not in labels.columns:
        return pd.DataFrame()

    common = features.index.intersection(labels.index)
    if not len(common):
        return pd.DataFrame()
    f = features.loc[common].select_dtypes(include=["number"])
    y = labels.loc[common, label_col]
    rows = []

    for col in f.columns:
        joined = pd.DataFrame({"x": f[col], "y": y}).dropna()
        if len(joined) < min_obs:
            continue
        row = {"feature": col, "n_obs": int(len(joined))}
        for method in methods:
            by_ts = joined.groupby(level="timestamp", group_keys=False).apply(
                lambda g: _corr_without_optional_deps(g, method)
            )
            by_ts = by_ts.dropna()
            row[f"{method}_ic_mean"] = float(by_ts.mean()) if not by_ts.empty else float("nan")
            row[f"{method}_ic_std"] = float(by_ts.std()) if len(by_ts) > 1 else float("nan")
            denom = row[f"{method}_ic_std"]
            row[f"{method}_ic_ir"] = row[f"{method}_ic_mean"] / denom if denom and pd.notna(denom) else float("nan")
            row[f"{method}_ic_abs_mean"] = abs(row[f"{method}_ic_mean"])
        rows.append(row)

    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows)
    return out.sort_values(["spearman_ic_abs_mean", "n_obs"], ascending=[False, False]).reset_index(drop=True)
