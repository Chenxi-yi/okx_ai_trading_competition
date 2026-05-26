"""Feature builders for persisted microstructure datasets."""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import numpy as np
import pandas as pd

from data.frame_store import read_frame


def build_microstructure_feature_panel(dataset_dir: Path, trade_freq: str = "1min") -> pd.DataFrame:
    """Build a feature panel from a dataset written by scripts/fetch_microstructure.py."""
    dataset_dir = Path(dataset_dir)
    frames: List[pd.DataFrame] = []
    for symbol_dir in sorted(p for p in dataset_dir.iterdir() if p.is_dir()):
        symbol = symbol_dir.name.replace("_", "/")
        parts = [
            _orderbook_features(_read(symbol_dir, "orderbook"), symbol),
            _trade_flow_features(_read(symbol_dir, "trades"), symbol, trade_freq),
            _series_features(_read(symbol_dir, "open_interest"), symbol, "oi"),
            _series_features(_read(symbol_dir, "funding"), symbol, "funding"),
            _series_features(_read(symbol_dir, "long_short"), symbol, "ls"),
        ]
        parts = [p for p in parts if p is not None and not p.empty]
        if not parts:
            continue
        panel = pd.concat(parts, axis=1).sort_index()
        panel["symbol"] = symbol
        panel = panel.set_index("symbol", append=True)
        panel.index.names = ["timestamp", "symbol"]
        frames.append(panel)

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames).sort_index().replace([np.inf, -np.inf], np.nan)


def _orderbook_features(df: Optional[pd.DataFrame], symbol: str) -> Optional[pd.DataFrame]:
    if df is None or df.empty:
        return None
    out = pd.DataFrame(index=_utc_index(df))
    for col in ("spread_bps", "depth_imbalance", "bid_notional_top", "ask_notional_top", "mid"):
        if col in df.columns:
            out[f"ob_{col}"] = pd.to_numeric(df[col], errors="coerce")
    if "bid_px_1" in df.columns and "ask_px_1" in df.columns:
        out["ob_microprice_proxy"] = (
            pd.to_numeric(df["bid_px_1"], errors="coerce") * pd.to_numeric(df.get("ask_sz_1", 0), errors="coerce")
            + pd.to_numeric(df["ask_px_1"], errors="coerce") * pd.to_numeric(df.get("bid_sz_1", 0), errors="coerce")
        ) / (
            pd.to_numeric(df.get("bid_sz_1", 0), errors="coerce")
            + pd.to_numeric(df.get("ask_sz_1", 0), errors="coerce")
        ).replace(0, np.nan)
    return out


def _trade_flow_features(df: Optional[pd.DataFrame], symbol: str, freq: str) -> Optional[pd.DataFrame]:
    if df is None or df.empty:
        return None
    raw = df.copy()
    raw.index = _utc_index(raw)
    raw["amount"] = pd.to_numeric(raw.get("amount", 0.0), errors="coerce").fillna(0.0)
    raw["cost"] = pd.to_numeric(raw.get("cost", 0.0), errors="coerce").fillna(0.0)
    raw["buy_cost"] = raw["cost"].where(raw.get("side") == "buy", 0.0)
    raw["sell_cost"] = raw["cost"].where(raw.get("side") == "sell", 0.0)
    grouped = raw.resample(freq)
    out = grouped.agg(
        trade_count=("cost", "count"),
        trade_notional=("cost", "sum"),
        buy_notional=("buy_cost", "sum"),
        sell_notional=("sell_cost", "sum"),
        avg_trade_notional=("cost", "mean"),
    )
    total = out["buy_notional"] + out["sell_notional"]
    out["trade_imbalance"] = (out["buy_notional"] - out["sell_notional"]) / total.replace(0, np.nan)
    out = out.add_prefix("tf_")
    return out


def _series_features(df: Optional[pd.DataFrame], symbol: str, prefix: str) -> Optional[pd.DataFrame]:
    if df is None or df.empty:
        return None
    out = pd.DataFrame(index=_utc_index(df))
    numeric = df.select_dtypes(include=["number"]).copy()
    for col in numeric.columns:
        out[f"{prefix}_{col}"] = pd.to_numeric(numeric[col], errors="coerce")
        out[f"{prefix}_{col}_chg_1"] = out[f"{prefix}_{col}"].pct_change()
    return out


def _read(symbol_dir: Path, kind: str) -> Optional[pd.DataFrame]:
    parquet = symbol_dir / f"{kind}.parquet"
    pickle = symbol_dir / f"{kind}.pkl"
    if parquet.exists():
        return read_frame(parquet)
    if pickle.exists():
        return pd.read_pickle(pickle)
    return None


def _utc_index(df: pd.DataFrame) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(pd.to_datetime(df.index, utc=True))
