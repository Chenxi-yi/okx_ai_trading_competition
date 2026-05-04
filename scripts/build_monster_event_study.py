#!/usr/bin/env python3
"""Create an event-study report for historical monster-coin moves."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from build_monster_dataset import (  # noqa: E402
    BARS,
    DEFAULT_EVENTS,
    DEFAULT_HISTORY_MANIFEST,
    OUT_ROOT,
    _load_symbol_data,
    _relpath,
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build historical monster event-study tables")
    p.add_argument("--history-manifest", default=str(DEFAULT_HISTORY_MANIFEST))
    p.add_argument("--events", default=str(DEFAULT_EVENTS))
    p.add_argument("--dataset-id", default="monster_event_study_5m_v1")
    p.add_argument("--timeframe", default="5m")
    p.add_argument("--top-n", type=int, default=120)
    p.add_argument("--exclude-market-events", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    manifest = json.loads(Path(args.history_manifest).read_text())
    data = _load_symbol_data(manifest["symbols"], args.timeframe)
    events = pd.read_parquet(args.events)
    events["event_ts"] = pd.to_datetime(events["event_ts"], utc=True)
    if args.exclude_market_events and "market_ret_1d" in events:
        events = events[events["market_ret_1d"].abs().fillna(0) < 0.12]
    events = events.sort_values("future_ret", ascending=False).head(args.top_n)

    rows = []
    for event in events.to_dict(orient="records"):
        sym = event["symbol"]
        if sym not in data:
            continue
        row = _study_event(event, data[sym].frame)
        if row:
            rows.append(row)

    study = pd.DataFrame(rows)
    if study.empty:
        raise SystemExit("No event-study rows built")
    summary = _summarize(study)
    out_dir = OUT_ROOT / args.dataset_id
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "event_study.csv"
    summary_path = out_dir / "phase_summary.csv"
    md_path = out_dir / "report.md"
    study.to_csv(csv_path, index=False)
    summary.to_csv(summary_path, index=False)
    md_path.write_text(_markdown_report(study, summary))
    payload = {
        "dataset_id": args.dataset_id,
        "created_at": pd.Timestamp.utcnow().isoformat(),
        "events": _relpath(Path(args.events)),
        "history_manifest": _relpath(Path(args.history_manifest)),
        "rows": int(len(study)),
        "exclude_market_events": bool(args.exclude_market_events),
        "artifacts": {
            "event_study_csv": _relpath(csv_path),
            "phase_summary_csv": _relpath(summary_path),
            "report_md": _relpath(md_path),
        },
    }
    (out_dir / "manifest.json").write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    print("\nPhase summary:")
    print(summary.to_string(index=False))
    print("\nTop studied events:")
    print(study.head(25).to_string(index=False))
    return 0


def _study_event(event: dict[str, Any], df: pd.DataFrame) -> dict[str, Any] | None:
    ts = pd.Timestamp(event["event_ts"])
    if ts not in df.index:
        pos = df.index.get_indexer([ts], method="nearest")[0]
        if pos < 0:
            return None
    else:
        pos = df.index.get_loc(ts)
    if not isinstance(pos, int) or pos < BARS["7d"] or pos + BARS["5d"] >= len(df):
        return None
    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"].fillna(0.0)
    px = close.iloc[pos]

    def ret_back(bars: int) -> float:
        return float(px / close.iloc[pos - bars] - 1.0)

    def ret_fwd(bars: int) -> float:
        return float(close.iloc[pos + bars] / px - 1.0)

    pre_24h = slice(pos - BARS["24h"], pos + 1)
    pre_7d = slice(pos - BARS["7d"], pos + 1)
    post_24h = close.iloc[pos + 1 : pos + BARS["24h"] + 1]
    post_3d = close.iloc[pos + 1 : pos + BARS["3d"] + 1]
    post_5d = close.iloc[pos + 1 : pos + BARS["5d"] + 1]
    first_24h_high = post_24h.max()
    after_first_high = post_3d.loc[post_3d.index >= post_24h.idxmax()]
    pullback_after_24h_high = float(after_first_high.min() / first_24h_high - 1.0) if len(after_first_high) else None

    return {
        "symbol": event["symbol"],
        "event_ts": ts.isoformat(),
        "horizon": event.get("horizon"),
        "future_ret": event.get("future_ret"),
        "market_ret_1d": event.get("market_ret_1d"),
        "idiosyncratic_ret_1d": event.get("idiosyncratic_ret_1d"),
        "pre_ret_7d": ret_back(BARS["7d"]),
        "pre_ret_3d": ret_back(BARS["3d"]),
        "pre_ret_24h": ret_back(BARS["24h"]),
        "pre_ret_6h": ret_back(BARS["6h"]),
        "pre_ret_1h": ret_back(BARS["1h"]),
        "pre_range_24h": float((high.iloc[pre_24h].max() - low.iloc[pre_24h].min()) / px),
        "pre_range_7d": float((high.iloc[pre_7d].max() - low.iloc[pre_7d].min()) / px),
        "pre_rvol_24h": float(close.pct_change(fill_method=None).iloc[pos - BARS["24h"] : pos].std()),
        "pre_vol_1h_vs_24h": float(
            volume.iloc[pos - BARS["1h"] : pos].mean() / volume.iloc[pos - BARS["24h"] : pos].mean()
        )
        if volume.iloc[pos - BARS["24h"] : pos].mean()
        else None,
        "post_ret_1h": ret_fwd(BARS["1h"]),
        "post_ret_6h": ret_fwd(BARS["6h"]),
        "post_ret_24h": ret_fwd(BARS["24h"]),
        "post_ret_3d": ret_fwd(BARS["3d"]),
        "max_ret_24h": float(post_24h.max() / px - 1.0),
        "max_ret_3d": float(post_3d.max() / px - 1.0),
        "max_ret_5d": float(post_5d.max() / px - 1.0),
        "min_ret_24h": float(post_24h.min() / px - 1.0),
        "pullback_after_24h_high": pullback_after_24h_high,
    }


def _summarize(study: pd.DataFrame) -> pd.DataFrame:
    cols = [
        "pre_ret_7d",
        "pre_ret_3d",
        "pre_ret_24h",
        "pre_ret_6h",
        "pre_ret_1h",
        "pre_range_24h",
        "pre_rvol_24h",
        "pre_vol_1h_vs_24h",
        "post_ret_1h",
        "post_ret_6h",
        "post_ret_24h",
        "post_ret_3d",
        "max_ret_24h",
        "max_ret_3d",
        "pullback_after_24h_high",
    ]
    rows = []
    for col in cols:
        s = pd.to_numeric(study[col], errors="coerce").dropna()
        rows.append(
            {
                "metric": col,
                "count": int(len(s)),
                "median": float(s.median()),
                "p25": float(s.quantile(0.25)),
                "p75": float(s.quantile(0.75)),
                "p90": float(s.quantile(0.90)),
            }
        )
    return pd.DataFrame(rows)


def _markdown_report(study: pd.DataFrame, summary: pd.DataFrame) -> str:
    top = study.head(20)[["symbol", "event_ts", "horizon", "future_ret", "pre_ret_24h", "post_ret_24h", "max_ret_5d"]]
    lines = [
        "# Monster Event Study v1",
        "",
        f"Rows: {len(study)}",
        "",
        "## Phase Summary",
        "",
        summary.to_markdown(index=False),
        "",
        "## Top Events",
        "",
        top.to_markdown(index=False),
        "",
        "## Interpretation Notes",
        "",
        "- This report is descriptive, not a tradable strategy by itself.",
        "- Use it to decide whether the scorer should chase first-leg momentum, wait for continuation, or avoid post-spike pullback zones.",
    ]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
