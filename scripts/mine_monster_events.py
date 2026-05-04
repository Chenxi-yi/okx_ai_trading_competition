#!/usr/bin/env python3
"""Mine monster-coin events from cached 5m OHLCV data."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
ENGINE_DIR = ROOT / "engine"
sys.path.insert(0, str(ENGINE_DIR))

OUT_ROOT = ENGINE_DIR / "data" / "monster_events"
DEFAULT_MANIFEST = ENGINE_DIR / "data" / "training_history" / "train_hist_134_5m_20240101_20260424" / "manifest.json"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Mine explosive return events from 5m OHLCV cache")
    p.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    p.add_argument("--dataset-id", default="monster_events_5m_v1")
    p.add_argument("--timeframe", default="5m")
    p.add_argument("--min-1d-ret", type=float, default=0.40)
    p.add_argument("--min-3d-ret", type=float, default=1.00)
    p.add_argument("--min-5d-ret", type=float, default=2.00)
    p.add_argument("--min-gap-hours", type=float, default=24.0)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    manifest = json.loads(Path(args.manifest).read_text())
    symbols = manifest["symbols"]
    series = _load_close_volume(symbols, args.timeframe)
    close = pd.concat({sym: data["close"] for sym, data in series.items()}, axis=1).sort_index()
    volume = pd.concat({sym: data["volume"] for sym, data in series.items()}, axis=1).sort_index()
    ret_panel_1d = close.pct_change(288, fill_method=None)
    ret_panel_6h = close.pct_change(72, fill_method=None)
    market_ret_1d = ret_panel_1d.median(axis=1, skipna=True)
    rank_panel_1d = ret_panel_1d.rank(axis=1, pct=True)
    rank_panel_6h = ret_panel_6h.rank(axis=1, pct=True)

    events: List[Dict[str, Any]] = []
    for sym, data in series.items():
        c = data["close"].dropna()
        v = data["volume"].reindex(c.index).fillna(0.0)
        if len(c) < 300:
            continue
        candidates = []
        for bars, name, threshold in [
            (288, "1d", args.min_1d_ret),
            (864, "3d", args.min_3d_ret),
            (1440, "5d", args.min_5d_ret),
            (2880, "10d", args.min_5d_ret),
        ]:
            if len(c) <= bars:
                continue
            future = c.shift(-bars)
            ret = future / c - 1.0
            hits = ret[ret >= threshold].dropna()
            for ts, value in hits.items():
                candidates.append((ts, name, bars, float(value), float(c.loc[ts]), float(future.loc[ts])))
        candidates.sort(key=lambda item: item[0])
        selected = _dedupe_events(candidates, args.min_gap_hours)
        for ts, horizon, bars, ret, start_px, end_px in selected:
            features = _pre_event_features(c, v, market_ret_1d, rank_panel_1d, rank_panel_6h, sym, ts)
            events.append(
                {
                    "symbol": sym,
                    "event_ts": ts.isoformat(),
                    "horizon": horizon,
                    "horizon_bars": bars,
                    "future_ret": ret,
                    "start_px": start_px,
                    "end_px": end_px,
                    **features,
                }
            )

    events_df = pd.DataFrame(events)
    if not events_df.empty:
        events_df = events_df.sort_values(["future_ret", "event_ts"], ascending=[False, True])
    out_dir = OUT_ROOT / args.dataset_id
    out_dir.mkdir(parents=True, exist_ok=True)
    events_path = out_dir / "events.parquet"
    try:
        events_df.to_parquet(events_path)
    except Exception:
        events_path = out_dir / "events.pkl"
        events_df.to_pickle(events_path)
    summary = _summary(events_df)
    payload = {
        "dataset_id": args.dataset_id,
        "manifest": _relpath(Path(args.manifest)),
        "symbols": len(symbols),
        "events": int(len(events_df)),
        "event_artifact": _relpath(events_path),
        "thresholds": {
            "min_1d_ret": args.min_1d_ret,
            "min_3d_ret": args.min_3d_ret,
            "min_5d_ret": args.min_5d_ret,
            "min_gap_hours": args.min_gap_hours,
        },
        "summary": summary,
    }
    (out_dir / "manifest.json").write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    if not events_df.empty:
        cols = ["symbol", "event_ts", "horizon", "future_ret", "pre_ret_1h", "pre_ret_6h", "pre_ret_24h", "pre_vol_z_24h", "market_ret_1d", "idiosyncratic_ret_1d"]
        print(events_df[cols].head(30).to_string(index=False))
    return 0


def _load_close_volume(symbols: list[str], timeframe: str) -> dict[str, dict[str, pd.Series]]:
    data = {}
    for sym in symbols:
        safe = sym.replace("/", "_")
        files = list((ENGINE_DIR / "data" / "cache").glob(f"{safe}_futures_{timeframe}.*"))
        if not files:
            continue
        path = files[0]
        df = pd.read_parquet(path) if path.suffix == ".parquet" else pd.read_pickle(path)
        if df.empty or "close" not in df:
            continue
        df = df.sort_index()
        data[sym] = {"close": df["close"].astype(float), "volume": df.get("volume", pd.Series(index=df.index, dtype=float)).astype(float)}
    return data


def _dedupe_events(candidates: list[tuple], min_gap_hours: float) -> list[tuple]:
    selected = []
    gap = pd.Timedelta(hours=min_gap_hours)
    for item in sorted(candidates, key=lambda x: (-x[3], x[0])):
        ts = item[0]
        if all(abs(ts - prev[0]) >= gap for prev in selected):
            selected.append(item)
    return sorted(selected, key=lambda x: x[0])


def _pre_event_features(
    c: pd.Series,
    v: pd.Series,
    market_ret_1d: pd.Series,
    rank_panel_1d: pd.DataFrame,
    rank_panel_6h: pd.DataFrame,
    sym: str,
    ts: pd.Timestamp,
) -> dict[str, Any]:
    loc = c.index.get_loc(ts)

    def ret_back(bars: int) -> float | None:
        if loc < bars:
            return None
        return float(c.iloc[loc] / c.iloc[loc - bars] - 1.0)

    def realized_vol(bars: int) -> float | None:
        if loc < bars:
            return None
        return float(c.pct_change().iloc[loc - bars:loc].std())

    vol_24 = v.rolling(288, min_periods=60).mean()
    vol_7d = v.rolling(2016, min_periods=200).mean()
    vol_std_7d = v.rolling(2016, min_periods=200).std()
    vol_z = (v - vol_7d) / vol_std_7d.replace(0, pd.NA)
    rel_strength = None
    try:
        market = float(market_ret_1d.reindex([ts], method="nearest").iloc[0])
        one_day = ret_back(288)
        rel_strength = None if one_day is None else float(one_day - market)
    except Exception:
        market = None

    age_days = float((ts - c.index.min()) / pd.Timedelta(days=1))
    return {
        "age_days": age_days,
        "pre_ret_1h": ret_back(12),
        "pre_ret_6h": ret_back(72),
        "pre_ret_24h": ret_back(288),
        "pre_ret_3d": ret_back(864),
        "pre_rvol_6h": realized_vol(72),
        "pre_rvol_24h": realized_vol(288),
        "pre_volume_24h_mean": float(vol_24.loc[ts]) if ts in vol_24.index and pd.notna(vol_24.loc[ts]) else None,
        "pre_volume_vs_7d": float(v.loc[ts] / vol_7d.loc[ts]) if ts in vol_7d.index and pd.notna(vol_7d.loc[ts]) and vol_7d.loc[ts] else None,
        "pre_vol_z_24h": float(vol_z.loc[ts]) if ts in vol_z.index and pd.notna(vol_z.loc[ts]) else None,
        "market_ret_1d": market,
        "idiosyncratic_ret_1d": rel_strength,
        "cross_section_rank_1d": _value_at(rank_panel_1d, sym, ts),
        "cross_section_rank_6h": _value_at(rank_panel_6h, sym, ts),
    }


def _value_at(panel: pd.DataFrame, sym: str, ts: pd.Timestamp) -> float | None:
    if sym not in panel.columns:
        return None
    try:
        value = panel.reindex([ts], method="nearest")[sym].iloc[0]
        return float(value) if pd.notna(value) else None
    except Exception:
        return None


def _summary(events_df: pd.DataFrame) -> dict[str, Any]:
    if events_df.empty:
        return {}
    return {
        "by_horizon": events_df["horizon"].value_counts().to_dict(),
        "top_symbols": events_df["symbol"].value_counts().head(20).to_dict(),
        "future_ret_quantiles": events_df["future_ret"].quantile([0.5, 0.75, 0.9, 0.99]).to_dict(),
    }


def _relpath(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


if __name__ == "__main__":
    raise SystemExit(main())
