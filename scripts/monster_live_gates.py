#!/usr/bin/env python3
"""Live snapshot gates for monster-coin candidates.

The gate consumes the latest candidate orderbook and derivatives snapshots.
It is intentionally independent from historical scoring because these live
features are not replayable over the full OHLCV backtest window yet.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT_ROOT = ROOT / "engine" / "data" / "monster_events"


@dataclass(frozen=True)
class LiveGateConfig:
    max_snapshot_age_minutes: float = 180.0
    max_spread_bps: float = 20.0
    min_depth_1pct_usd: float = 10_000.0
    min_open_interest_value: float = 1_000_000.0
    max_abs_funding_rate: float = 0.0015
    max_long_short_ratio: float = 4.0
    min_long_short_ratio: float = 0.25


def build_live_gate_table(config: LiveGateConfig | None = None, now: pd.Timestamp | None = None) -> pd.DataFrame:
    cfg = config or LiveGateConfig()
    now = now or pd.Timestamp.utcnow()
    orderbook = latest_snapshot("monster_orderbook_", "orderbook_features")
    derivatives = latest_snapshot("monster_derivatives_", "derivatives_features")
    symbols = sorted(set(_symbols(orderbook)) | set(_symbols(derivatives)))
    rows: list[dict[str, Any]] = []
    for symbol in symbols:
        ob = _row_for_symbol(orderbook, symbol)
        dv = _row_for_symbol(derivatives, symbol)
        row: dict[str, Any] = {"symbol": symbol}
        row.update(_prefixed("ob", ob))
        row.update(_prefixed("deriv", dv))
        verdict = live_gate_verdict(row, cfg, now)
        row.update(verdict)
        rows.append(row)
    if not rows:
        return pd.DataFrame(columns=["symbol", "live_gate_flag", "live_gate_reasons"])
    return pd.DataFrame(rows).sort_values(["live_gate_flag", "symbol"], ascending=[False, True])


def latest_snapshot(prefix: str, stem: str) -> pd.DataFrame:
    run_dir = _latest_run(prefix, stem)
    if run_dir is None:
        return pd.DataFrame()
    parquet = run_dir / f"{stem}.parquet"
    csv = run_dir / f"{stem}.csv"
    jsonl = run_dir / f"{stem}.jsonl"
    if parquet.exists():
        df = pd.read_parquet(parquet)
    elif csv.exists():
        df = pd.read_csv(csv)
    elif jsonl.exists():
        rows = []
        for line in jsonl.read_text().splitlines():
            if line.strip():
                rows.append(json.loads(line))
        df = pd.DataFrame(rows)
    else:
        return pd.DataFrame()
    if df.empty or "symbol" not in df:
        return pd.DataFrame()
    if "ts" in df:
        df["_ts_sort"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
        df = df.sort_values("_ts_sort")
    latest = df.drop_duplicates(subset=["symbol"], keep="last").copy()
    latest["source_run_id"] = run_dir.name
    return latest.drop(columns=[c for c in ["_ts_sort"] if c in latest], errors="ignore")


def live_gate_verdict(row: dict[str, Any], config: LiveGateConfig, now: pd.Timestamp) -> dict[str, Any]:
    reasons: list[str] = []
    warnings: list[str] = []

    ob_ts = _timestamp(row.get("ob_ts"))
    deriv_ts = _timestamp(row.get("deriv_ts"))
    if ob_ts is None:
        reasons.append("missing_orderbook")
    elif _age_minutes(now, ob_ts) > config.max_snapshot_age_minutes:
        reasons.append(f"orderbook_stale>{config.max_snapshot_age_minutes:g}m")
    if deriv_ts is None:
        reasons.append("missing_derivatives")
    elif _age_minutes(now, deriv_ts) > config.max_snapshot_age_minutes:
        reasons.append(f"derivatives_stale>{config.max_snapshot_age_minutes:g}m")

    spread = _float(row.get("ob_spread_bps"))
    if spread is None:
        reasons.append("missing_spread")
    elif spread > config.max_spread_bps:
        reasons.append(f"spread>{config.max_spread_bps:g}bps")

    depth = _float(row.get("ob_depth_1pct_usd"))
    if depth is None:
        reasons.append("missing_depth_1pct")
    elif depth < config.min_depth_1pct_usd:
        reasons.append(f"depth_1pct<{config.min_depth_1pct_usd:g}")

    oi_value = _float(row.get("deriv_open_interest_value"))
    if oi_value is None:
        reasons.append("missing_open_interest")
    elif oi_value < config.min_open_interest_value:
        reasons.append(f"oi_value<{config.min_open_interest_value:g}")

    funding = _float(row.get("deriv_funding_rate"))
    if funding is None:
        warnings.append("missing_funding")
    elif abs(funding) > config.max_abs_funding_rate:
        reasons.append(f"abs_funding>{config.max_abs_funding_rate:g}")

    long_short = _float(row.get("deriv_long_short_ratio"))
    if long_short is None:
        warnings.append("missing_long_short")
    elif long_short > config.max_long_short_ratio:
        reasons.append(f"long_short>{config.max_long_short_ratio:g}")
    elif long_short < config.min_long_short_ratio:
        reasons.append(f"long_short<{config.min_long_short_ratio:g}")

    return {
        "live_gate_flag": int(not reasons),
        "live_gate_reasons": "; ".join(reasons),
        "live_gate_warnings": "; ".join(warnings),
    }


def _latest_run(prefix: str, stem: str) -> Path | None:
    if not OUT_ROOT.exists():
        return None
    candidates = [p for p in OUT_ROOT.iterdir() if p.is_dir() and p.name.startswith(prefix) and _has_artifact(p, stem)]
    if not candidates:
        return None
    return max(candidates, key=lambda p: _mtime(p, stem))


def _has_artifact(path: Path, stem: str) -> bool:
    return any((path / f"{stem}{suffix}").exists() for suffix in [".parquet", ".csv", ".jsonl"])


def _mtime(path: Path, stem: str) -> float:
    mtimes = [(path / f"{stem}{suffix}").stat().st_mtime for suffix in [".parquet", ".csv", ".jsonl"] if (path / f"{stem}{suffix}").exists()]
    return max(mtimes) if mtimes else path.stat().st_mtime


def _symbols(df: pd.DataFrame) -> list[str]:
    if df.empty or "symbol" not in df:
        return []
    return df["symbol"].dropna().astype(str).tolist()


def _row_for_symbol(df: pd.DataFrame, symbol: str) -> dict[str, Any]:
    if df.empty or "symbol" not in df:
        return {}
    rows = df[df["symbol"].astype(str) == symbol]
    if rows.empty:
        return {}
    return rows.iloc[-1].to_dict()


def _prefixed(prefix: str, row: dict[str, Any]) -> dict[str, Any]:
    return {f"{prefix}_{k}": v for k, v in row.items() if k != "symbol"}


def _timestamp(value: Any) -> pd.Timestamp | None:
    if value in (None, ""):
        return None
    ts = pd.Timestamp(value)
    if pd.isna(ts):
        return None
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _age_minutes(now: pd.Timestamp, ts: pd.Timestamp) -> float:
    now = now.tz_localize("UTC") if now.tzinfo is None else now.tz_convert("UTC")
    return float((now - ts) / pd.Timedelta(minutes=1))


def _float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        if pd.isna(value):
            return None
        return float(value)
    except Exception:
        return None
