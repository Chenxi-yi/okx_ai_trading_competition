"""Forward-looking labels for feature research."""

from __future__ import annotations

from typing import Dict, Iterable, List

import numpy as np
import pandas as pd


DEFAULT_LABEL_HORIZONS = (1, 3, 6, 12, 24)


def build_label_panel(
    price_data: Dict[str, pd.DataFrame],
    horizons: Iterable[int] = DEFAULT_LABEL_HORIZONS,
    fee_bps: float = 5.0,
    slippage_bps: float = 2.0,
    funding_cost_bps_per_bar: float = 0.0,
) -> pd.DataFrame:
    """Build forward-return, MFE, MAE, and direction labels."""
    round_trip_cost = 2.0 * (fee_bps + slippage_bps) / 10_000.0
    frames: List[pd.DataFrame] = []
    for symbol, raw in sorted(price_data.items()):
        if raw is None or raw.empty:
            continue
        df = raw.copy()
        df.index = pd.to_datetime(df.index, utc=True)
        df = df.sort_index()
        close = pd.to_numeric(df["close"], errors="coerce")
        high = pd.to_numeric(df.get("high", close), errors="coerce")
        low = pd.to_numeric(df.get("low", close), errors="coerce")

        labels = pd.DataFrame(index=df.index)
        for h in horizons:
            fwd = close.shift(-h) / close - 1.0
            horizon_cost = round_trip_cost + h * funding_cost_bps_per_bar / 10_000.0
            labels[f"fwd_ret_{h}"] = fwd
            labels[f"fwd_ret_net_long_{h}"] = fwd - horizon_cost
            labels[f"fwd_ret_net_short_{h}"] = -fwd - horizon_cost
            labels[f"fwd_abs_edge_after_cost_{h}"] = fwd.abs() - horizon_cost
            labels[f"fwd_dir_{h}"] = np.sign(fwd)
            labels[f"mfe_{h}"] = _future_rolling(high, h, "max") / close - 1.0
            labels[f"mae_{h}"] = _future_rolling(low, h, "min") / close - 1.0
            labels[f"hit_1pct_before_down_1pct_{h}"] = _hit_before(close, high, low, h, up=0.01, down=0.01)

        labels["symbol"] = symbol
        labels = labels.set_index("symbol", append=True)
        labels.index.names = ["timestamp", "symbol"]
        frames.append(labels)

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames).sort_index().replace([np.inf, -np.inf], np.nan)


def _future_rolling(series: pd.Series, horizon: int, op: str) -> pd.Series:
    shifted = series.shift(-1)
    rolled = shifted.iloc[::-1].rolling(horizon, min_periods=1)
    if op == "max":
        return rolled.max().iloc[::-1]
    if op == "min":
        return rolled.min().iloc[::-1]
    raise ValueError(f"Unknown future rolling op: {op}")


def _hit_before(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    horizon: int,
    up: float,
    down: float,
) -> pd.Series:
    out = pd.Series(np.nan, index=close.index)
    values_close = close.to_numpy(dtype=float)
    values_high = high.to_numpy(dtype=float)
    values_low = low.to_numpy(dtype=float)
    n = len(close)
    for i in range(n):
        base = values_close[i]
        if not np.isfinite(base) or base <= 0:
            continue
        upper = base * (1 + up)
        lower = base * (1 - down)
        end = min(n, i + horizon + 1)
        for j in range(i + 1, end):
            if values_low[j] <= lower:
                out.iloc[i] = 0.0
                break
            if values_high[j] >= upper:
                out.iloc[i] = 1.0
                break
    return out
