#!/usr/bin/env python3
"""Build point-in-time monster-coin research samples from cached 5m data."""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
ENGINE_DIR = ROOT / "engine"
sys.path.insert(0, str(ENGINE_DIR))

DEFAULT_HISTORY_MANIFEST = (
    ENGINE_DIR / "data" / "training_history" / "train_hist_134_5m_20240101_20260424" / "manifest.json"
)
DEFAULT_EVENTS = ENGINE_DIR / "data" / "monster_events" / "monster_events_5m_v1" / "events.parquet"
OUT_ROOT = ENGINE_DIR / "data" / "monster_events"

BAR_MINUTES = 5
BARS = {
    "15m": 3,
    "1h": 12,
    "3h": 36,
    "6h": 72,
    "12h": 144,
    "24h": 288,
    "3d": 864,
    "5d": 1440,
    "7d": 2016,
}


@dataclass
class SymbolData:
    frame: pd.DataFrame
    features: dict[str, pd.Series]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build positive/negative samples for monster-coin research")
    p.add_argument("--history-manifest", default=str(DEFAULT_HISTORY_MANIFEST))
    p.add_argument("--events", default=str(DEFAULT_EVENTS))
    p.add_argument("--dataset-id", default="monster_samples_5m_v1")
    p.add_argument("--timeframe", default="5m")
    p.add_argument("--negatives-per-positive", type=int, default=5)
    p.add_argument("--exclude-hours-around-positive", type=float, default=72.0)
    p.add_argument("--seed", type=int, default=20260426)
    p.add_argument("--min-history-days", type=float, default=8.0)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    manifest = json.loads(Path(args.history_manifest).read_text())
    symbols = manifest["symbols"]
    data = _load_symbol_data(symbols, args.timeframe)
    if not data:
        raise SystemExit("No cached OHLCV data loaded")

    close_panel = pd.concat({sym: item.frame["close"] for sym, item in data.items()}, axis=1).sort_index()
    market = _market_panels(close_panel)

    events = pd.read_parquet(args.events)
    events["event_ts"] = pd.to_datetime(events["event_ts"], utc=True)
    positives = _positive_samples(events, data, market)
    negatives = _negative_samples(
        data=data,
        events=events,
        market=market,
        n_target=len(positives) * args.negatives_per_positive,
        exclude_hours=args.exclude_hours_around_positive,
        min_history_days=args.min_history_days,
        seed=args.seed,
    )
    samples = pd.DataFrame(positives + negatives)
    if samples.empty:
        raise SystemExit("No samples built")

    samples = samples.sort_values(["sample_ts", "symbol", "label_monster"], ascending=[True, True, False])
    feature_summary = _feature_summary(samples)
    out_dir = OUT_ROOT / args.dataset_id
    out_dir.mkdir(parents=True, exist_ok=True)
    samples_path = out_dir / "samples.parquet"
    summary_path = out_dir / "feature_summary.csv"
    samples.to_parquet(samples_path)
    feature_summary.to_csv(summary_path, index=False)

    payload = {
        "dataset_id": args.dataset_id,
        "created_at": pd.Timestamp.utcnow().isoformat(),
        "history_manifest": _relpath(Path(args.history_manifest)),
        "events": _relpath(Path(args.events)),
        "symbols_requested": len(symbols),
        "symbols_loaded": len(data),
        "rows": int(len(samples)),
        "positive_rows": int(samples["label_monster"].sum()),
        "negative_rows": int((samples["label_monster"] == 0).sum()),
        "feature_count": int(len(_feature_columns(samples))),
        "samples_artifact": _relpath(samples_path),
        "feature_summary_artifact": _relpath(summary_path),
        "negative_sampling": {
            "negatives_per_positive": args.negatives_per_positive,
            "exclude_hours_around_positive": args.exclude_hours_around_positive,
            "seed": args.seed,
            "min_history_days": args.min_history_days,
        },
        "label_definition": {
            "positive_source": "mined explosive events: 1d>=40%, 3d>=100%, 5d/10d>=200%",
            "negative_filter": "not close to positives and fwd_1d<20%, fwd_3d<50%, fwd_5d<100%",
        },
        "top_features_by_auc_distance": feature_summary.head(25).to_dict(orient="records"),
    }
    (out_dir / "manifest.json").write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    print("\nTop feature diagnostics:")
    print(feature_summary.head(30).to_string(index=False))
    return 0


def _load_symbol_data(symbols: list[str], timeframe: str) -> dict[str, SymbolData]:
    loaded: dict[str, SymbolData] = {}
    for sym in symbols:
        safe = sym.replace("/", "_")
        files = sorted((ENGINE_DIR / "data" / "cache").glob(f"{safe}_futures_{timeframe}.*"))
        if not files:
            continue
        path = next((p for p in files if p.suffix == ".parquet"), files[0])
        df = pd.read_parquet(path) if path.suffix == ".parquet" else pd.read_pickle(path)
        if df.empty or "close" not in df:
            continue
        df = df.sort_index()
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        else:
            df.index = df.index.tz_convert("UTC")
        for col in ["open", "high", "low", "close", "volume"]:
            if col not in df:
                df[col] = df["close"] if col != "volume" else 0.0
            df[col] = pd.to_numeric(df[col], errors="coerce").astype(float)
        df = df[["open", "high", "low", "close", "volume"]].dropna(subset=["close"])
        if len(df) < BARS["7d"] + BARS["5d"]:
            continue
        loaded[sym] = SymbolData(frame=df, features=_precompute_symbol_features(df))
    return loaded


def _precompute_symbol_features(df: pd.DataFrame) -> dict[str, pd.Series]:
    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"].fillna(0.0)
    ret_1 = close.pct_change(fill_method=None)
    features: dict[str, pd.Series] = {}
    for name, bars in BARS.items():
        features[f"ret_{name}"] = close.pct_change(bars, fill_method=None)
        features[f"rvol_{name}"] = ret_1.rolling(bars, min_periods=max(3, bars // 4)).std()
        roll_high = high.rolling(bars, min_periods=max(3, bars // 4)).max()
        roll_low = low.rolling(bars, min_periods=max(3, bars // 4)).min()
        span = (roll_high - roll_low).replace(0, pd.NA)
        features[f"dist_high_{name}"] = close / roll_high - 1.0
        features[f"range_pos_{name}"] = (close - roll_low) / span
        features[f"range_pct_{name}"] = span / close
        features[f"volume_sum_{name}"] = volume.rolling(bars, min_periods=max(3, bars // 4)).sum()
        features[f"volume_mean_{name}"] = volume.rolling(bars, min_periods=max(3, bars // 4)).mean()

    vol_7d = volume.rolling(BARS["7d"], min_periods=300).mean()
    vol_7d_std = volume.rolling(BARS["7d"], min_periods=300).std()
    features["volume_vs_7d"] = volume / vol_7d.replace(0, pd.NA)
    features["volume_z_7d"] = (volume - vol_7d) / vol_7d_std.replace(0, pd.NA)
    features["volume_1h_vs_24h"] = features["volume_mean_1h"] / features["volume_mean_24h"].replace(0, pd.NA)
    features["volume_6h_vs_7d"] = features["volume_mean_6h"] / features["volume_mean_7d"].replace(0, pd.NA)
    features["range_6h_vs_7d"] = features["range_pct_6h"] / features["range_pct_7d"].replace(0, pd.NA)
    features["range_24h_vs_7d"] = features["range_pct_24h"] / features["range_pct_7d"].replace(0, pd.NA)
    return features


def _market_panels(close: pd.DataFrame) -> dict[str, pd.DataFrame | pd.Series]:
    panels: dict[str, pd.DataFrame | pd.Series] = {}
    for name, bars in [("1h", 12), ("6h", 72), ("24h", 288), ("3d", 864)]:
        ret = close.pct_change(bars, fill_method=None)
        panels[f"ret_{name}"] = ret
        panels[f"median_ret_{name}"] = ret.median(axis=1, skipna=True)
        panels[f"rank_ret_{name}"] = ret.rank(axis=1, pct=True)
    ret_24h = panels["ret_24h"]
    assert isinstance(ret_24h, pd.DataFrame)
    panels["breadth_up_10_24h"] = (ret_24h >= 0.10).mean(axis=1, skipna=True)
    panels["breadth_up_20_24h"] = (ret_24h >= 0.20).mean(axis=1, skipna=True)
    panels["breadth_down_10_24h"] = (ret_24h <= -0.10).mean(axis=1, skipna=True)
    return panels


def _positive_samples(events: pd.DataFrame, data: dict[str, SymbolData], market: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for row in events.to_dict(orient="records"):
        sym = row["symbol"]
        ts = pd.Timestamp(row["event_ts"])
        if sym not in data:
            continue
        built = _sample_row(sym, ts, data[sym], market)
        if not built:
            continue
        built.update(
            {
                "label_monster": 1,
                "sample_type": "positive",
                "event_horizon": row.get("horizon"),
                "event_future_ret": row.get("future_ret"),
            }
        )
        rows.append(built)
    return rows


def _negative_samples(
    data: dict[str, SymbolData],
    events: pd.DataFrame,
    market: dict[str, Any],
    n_target: int,
    exclude_hours: float,
    min_history_days: float,
    seed: int,
) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    by_symbol: dict[str, list[pd.Timestamp]] = {}
    for sym, group in events.groupby("symbol"):
        by_symbol[sym] = sorted(pd.Timestamp(x) for x in group["event_ts"])

    symbols = list(data)
    rows: list[dict[str, Any]] = []
    attempts = 0
    max_attempts = max(50000, n_target * 80)
    min_loc = int((min_history_days * 24 * 60) / BAR_MINUTES)
    max_forward = BARS["5d"]
    gap = pd.Timedelta(hours=exclude_hours)
    seen: set[tuple[str, pd.Timestamp]] = set()

    while len(rows) < n_target and attempts < max_attempts:
        attempts += 1
        sym = rng.choice(symbols)
        item = data[sym]
        idx = item.frame.index
        if len(idx) <= min_loc + max_forward + 1:
            continue
        loc = rng.randrange(min_loc, len(idx) - max_forward)
        ts = idx[loc]
        key = (sym, ts)
        if key in seen:
            continue
        seen.add(key)
        if any(abs(ts - event_ts) <= gap for event_ts in by_symbol.get(sym, [])):
            continue
        fwd = _forward_returns(item.frame["close"], loc)
        if fwd["fwd_ret_1d"] >= 0.20 or fwd["fwd_ret_3d"] >= 0.50 or fwd["fwd_ret_5d"] >= 1.00:
            continue
        built = _sample_row(sym, ts, item, market)
        if not built:
            continue
        built.update({"label_monster": 0, "sample_type": "negative", "event_horizon": None, "event_future_ret": None})
        rows.append(built)
    return rows


def _sample_row(
    sym: str,
    ts: pd.Timestamp,
    item: SymbolData,
    market: dict[str, Any],
    require_forward: bool = True,
) -> dict[str, Any] | None:
    df = item.frame
    if ts not in df.index:
        pos = df.index.get_indexer([ts], method="nearest")[0]
        if pos < 0:
            return None
        ts = df.index[pos]
    loc = df.index.get_loc(ts)
    if not isinstance(loc, int) or loc < BARS["7d"]:
        return None
    if require_forward and loc + BARS["5d"] >= len(df):
        return None
    row: dict[str, Any] = {
        "symbol": sym,
        "sample_ts": ts.isoformat(),
        "age_days": float((ts - df.index.min()) / pd.Timedelta(days=1)),
        "close": float(df["close"].iloc[loc]),
        "volume": float(df["volume"].iloc[loc]),
    }
    if require_forward:
        row.update(_forward_returns(df["close"], loc))
    for name, series in item.features.items():
        row[name] = _to_float(series.iloc[loc])

    for name in ["1h", "6h", "24h", "3d"]:
        median = market[f"median_ret_{name}"]
        rank = market[f"rank_ret_{name}"]
        assert isinstance(median, pd.Series)
        assert isinstance(rank, pd.DataFrame)
        market_ret = _series_at(median, ts)
        row[f"market_ret_{name}"] = market_ret
        row[f"idio_ret_{name}"] = None if market_ret is None or row.get(f"ret_{name}") is None else row[f"ret_{name}"] - market_ret
        row[f"cs_rank_ret_{name}"] = _panel_at(rank, sym, ts)
    for name in ["breadth_up_10_24h", "breadth_up_20_24h", "breadth_down_10_24h"]:
        series = market[name]
        assert isinstance(series, pd.Series)
        row[name] = _series_at(series, ts)
    row["market_event_flag"] = int(
        (row.get("market_ret_24h") is not None and abs(row["market_ret_24h"]) >= 0.12)
        or (row.get("breadth_up_20_24h") is not None and row["breadth_up_20_24h"] >= 0.20)
        or (row.get("breadth_down_10_24h") is not None and row["breadth_down_10_24h"] >= 0.35)
    )
    return row


def _forward_returns(close: pd.Series, loc: int) -> dict[str, float]:
    px = close.iloc[loc]
    out: dict[str, float] = {}
    for name in ["1d", "3d", "5d"]:
        bars = BARS["24h"] if name == "1d" else BARS[name]
        out[f"fwd_ret_{name}"] = float(close.iloc[loc + bars] / px - 1.0)
    future = close.iloc[loc + 1 : loc + BARS["5d"] + 1]
    out["max_fwd_ret_5d"] = float(future.max() / px - 1.0)
    out["min_fwd_ret_5d"] = float(future.min() / px - 1.0)
    return out


def _feature_columns(samples: pd.DataFrame) -> list[str]:
    excluded = {
        "symbol",
        "sample_ts",
        "sample_type",
        "label_monster",
        "event_horizon",
        "event_future_ret",
        "fwd_ret_1d",
        "fwd_ret_3d",
        "fwd_ret_5d",
        "max_fwd_ret_5d",
        "min_fwd_ret_5d",
        "close",
        "volume",
    }
    return [c for c in samples.columns if c not in excluded and pd.api.types.is_numeric_dtype(samples[c])]


def _feature_summary(samples: pd.DataFrame) -> pd.DataFrame:
    y = samples["label_monster"].astype(int)
    rows = []
    for col in _feature_columns(samples):
        s = pd.to_numeric(samples[col], errors="coerce")
        pos = s[y == 1].dropna()
        neg = s[y == 0].dropna()
        if len(pos) < 20 or len(neg) < 20:
            continue
        pos_mean = float(pos.mean())
        neg_mean = float(neg.mean())
        pooled = math.sqrt((float(pos.var(ddof=0)) + float(neg.var(ddof=0))) / 2.0)
        separation = None if pooled == 0 or math.isnan(pooled) else (pos_mean - neg_mean) / pooled
        auc = _auc_score(s, y)
        rows.append(
            {
                "feature": col,
                "positive_mean": pos_mean,
                "negative_mean": neg_mean,
                "positive_median": float(pos.median()),
                "negative_median": float(neg.median()),
                "positive_nan_rate": float(1.0 - len(pos) / max(1, int((y == 1).sum()))),
                "negative_nan_rate": float(1.0 - len(neg) / max(1, int((y == 0).sum()))),
                "separation_z": separation,
                "auc": auc,
                "auc_distance": None if auc is None else abs(auc - 0.5),
            }
        )
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    return out.sort_values(["auc_distance", "separation_z"], ascending=[False, False], na_position="last")


def _auc_score(values: pd.Series, labels: pd.Series) -> float | None:
    df = pd.DataFrame({"x": values, "y": labels}).dropna()
    n_pos = int((df["y"] == 1).sum())
    n_neg = int((df["y"] == 0).sum())
    if n_pos == 0 or n_neg == 0:
        return None
    ranks = df["x"].rank(method="average")
    rank_sum_pos = float(ranks[df["y"] == 1].sum())
    return (rank_sum_pos - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def _series_at(series: pd.Series, ts: pd.Timestamp) -> float | None:
    try:
        if ts in series.index:
            return _to_float(series.loc[ts])
        pos = series.index.get_indexer([ts], method="nearest")[0]
        if pos < 0:
            return None
        return _to_float(series.iloc[pos])
    except Exception:
        return None


def _panel_at(panel: pd.DataFrame, sym: str, ts: pd.Timestamp) -> float | None:
    if sym not in panel.columns:
        return None
    return _series_at(panel[sym], ts)


def _to_float(value: Any) -> float | None:
    try:
        if pd.isna(value):
            return None
        return float(value)
    except Exception:
        return None


def _relpath(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


if __name__ == "__main__":
    raise SystemExit(main())
