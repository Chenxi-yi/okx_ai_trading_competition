#!/usr/bin/env python3
"""Cluster mined monster events into independent symbol episodes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
ENGINE_DIR = ROOT / "engine"
OUT_ROOT = ENGINE_DIR / "data" / "monster_events"
DEFAULT_EVENTS = OUT_ROOT / "monster_events_5m_v1" / "events.parquet"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Cluster repeated monster labels into episodes")
    p.add_argument("--events", default=str(DEFAULT_EVENTS))
    p.add_argument("--dataset-id", default="monster_episodes_5m_v1")
    p.add_argument("--cluster-gap-days", type=float, default=10.0)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    events = pd.read_parquet(args.events)
    events["event_ts"] = pd.to_datetime(events["event_ts"], utc=True)
    episodes = _cluster(events, pd.Timedelta(days=args.cluster_gap_days))
    out_dir = OUT_ROOT / args.dataset_id
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "episodes.parquet"
    episodes.to_parquet(path)
    payload = {
        "dataset_id": args.dataset_id,
        "created_at": pd.Timestamp.utcnow().isoformat(),
        "source_events": _relpath(Path(args.events)),
        "source_event_rows": int(len(events)),
        "episodes": int(len(episodes)),
        "cluster_gap_days": args.cluster_gap_days,
        "artifact": _relpath(path),
        "top_symbols": episodes["symbol"].value_counts().head(20).to_dict(),
    }
    (out_dir / "manifest.json").write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    print(episodes.head(40).to_string(index=False))
    return 0


def _cluster(events: pd.DataFrame, gap: pd.Timedelta) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for sym, group in events.sort_values("event_ts").groupby("symbol"):
        cluster: list[dict[str, Any]] = []
        cluster_until: pd.Timestamp | None = None
        for row in group.to_dict(orient="records"):
            ts = pd.Timestamp(row["event_ts"])
            if not cluster or (cluster_until is not None and ts <= cluster_until):
                cluster.append(row)
            else:
                rows.append(_episode(sym, cluster))
                cluster = [row]
            cluster_until = max(cluster_until or ts, ts + gap)
        if cluster:
            rows.append(_episode(sym, cluster))
    episodes = pd.DataFrame(rows)
    if not episodes.empty:
        episodes = episodes.sort_values(["future_ret", "event_ts"], ascending=[False, True])
    return episodes


def _episode(sym: str, cluster: list[dict[str, Any]]) -> dict[str, Any]:
    best = max(cluster, key=lambda row: float(row.get("future_ret") or 0.0))
    first = min(cluster, key=lambda row: pd.Timestamp(row["event_ts"]))
    last = max(cluster, key=lambda row: pd.Timestamp(row["event_ts"]))
    event_ts = pd.Timestamp(first["event_ts"])
    return {
        "symbol": sym,
        "event_ts": event_ts.isoformat(),
        "episode_start_ts": event_ts.isoformat(),
        "episode_last_label_ts": pd.Timestamp(last["event_ts"]).isoformat(),
        "horizon": best.get("horizon"),
        "horizon_bars": best.get("horizon_bars"),
        "future_ret": best.get("future_ret"),
        "event_count": len(cluster),
        "best_label_ts": pd.Timestamp(best["event_ts"]).isoformat(),
        "market_ret_1d": first.get("market_ret_1d"),
        "idiosyncratic_ret_1d": first.get("idiosyncratic_ret_1d"),
    }


def _relpath(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


if __name__ == "__main__":
    raise SystemExit(main())
