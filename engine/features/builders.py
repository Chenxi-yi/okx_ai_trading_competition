"""
Feature builders for research datasets.

All outputs are point-in-time panels indexed by (timestamp, symbol). Builders
must only use data at or before each timestamp.
"""

from __future__ import annotations

from typing import Dict, Iterable, List

import numpy as np
import pandas as pd


DEFAULT_RETURN_WINDOWS = (1, 3, 6, 12, 24, 72)
DEFAULT_ROLLING_WINDOWS = (12, 24, 72)


def build_feature_panel(
    price_data: Dict[str, pd.DataFrame],
    return_windows: Iterable[int] = DEFAULT_RETURN_WINDOWS,
    rolling_windows: Iterable[int] = DEFAULT_ROLLING_WINDOWS,
) -> pd.DataFrame:
    """
    Build a feature panel from OHLCV/funding data.

    Parameters
    ----------
    price_data:
        Mapping of symbol -> DataFrame with at least open/high/low/close/volume.
    return_windows:
        Bar horizons for backward-looking returns.
    rolling_windows:
        Bar windows for rolling stats.
    """
    frames: List[pd.DataFrame] = []
    for symbol, raw in sorted(price_data.items()):
        if raw is None or raw.empty:
            continue
        df = _normalize_input(raw)
        if df.empty or "close" not in df:
            continue
        feats = _features_for_symbol(df, return_windows, rolling_windows)
        feats["symbol"] = symbol
        feats = feats.set_index("symbol", append=True)
        feats.index.names = ["timestamp", "symbol"]
        frames.append(feats)

    if not frames:
        return pd.DataFrame()

    panel = pd.concat(frames).sort_index()
    return panel.replace([np.inf, -np.inf], np.nan)


def _normalize_input(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out.index = pd.to_datetime(out.index, utc=True)
    out = out.sort_index()
    out = out[~out.index.duplicated(keep="last")]
    for col in ("open", "high", "low", "close", "volume", "funding_rate"):
        if col not in out.columns:
            out[col] = 0.0 if col in ("volume", "funding_rate") else np.nan
        out[col] = pd.to_numeric(out[col], errors="coerce")
    return out


def _features_for_symbol(
    df: pd.DataFrame,
    return_windows: Iterable[int],
    rolling_windows: Iterable[int],
) -> pd.DataFrame:
    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"].replace(0, np.nan)
    funding = df["funding_rate"].fillna(0.0)
    ret_1 = close.pct_change()

    feats = pd.DataFrame(index=df.index)
    feats["close"] = close
    feats["log_price"] = np.log(close.where(close > 0))
    feats["range_pct"] = (high - low) / close
    feats["close_to_high"] = close / high.replace(0, np.nan) - 1.0
    feats["close_to_low"] = close / low.replace(0, np.nan) - 1.0
    feats["volume_usd"] = volume * close
    feats["funding_rate"] = funding

    for window in return_windows:
        feats[f"ret_{window}"] = close.pct_change(window)
        feats[f"mom_z_{window}"] = _rolling_z(feats[f"ret_{window}"], max(window * 4, 20))

    for window in rolling_windows:
        vol = ret_1.rolling(window, min_periods=max(3, window // 3)).std()
        feats[f"rv_{window}"] = vol
        feats[f"vol_z_{window}"] = _rolling_z(volume, window)
        feats[f"funding_mean_{window}"] = funding.rolling(window, min_periods=max(3, window // 3)).mean()
        feats[f"funding_z_{window}"] = _rolling_z(funding, window)
        feats[f"range_mean_{window}"] = feats["range_pct"].rolling(window, min_periods=max(3, window // 3)).mean()
        feats[f"trend_eff_{window}"] = close.pct_change(window) / (ret_1.abs().rolling(window, min_periods=max(3, window // 3)).sum())

    feats["atr_14_pct"] = _atr(high, low, close, 14) / close
    feats["ret_1_abs"] = ret_1.abs()
    feats["downside_rv_24"] = ret_1.where(ret_1 < 0, 0.0).rolling(24, min_periods=8).std()
    return feats


def _rolling_z(series: pd.Series, window: int) -> pd.Series:
    mean = series.rolling(window, min_periods=max(3, window // 3)).mean()
    std = series.rolling(window, min_periods=max(3, window // 3)).std()
    return (series - mean) / std.replace(0, np.nan)


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, window: int) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(window, min_periods=max(3, window // 3)).mean()
