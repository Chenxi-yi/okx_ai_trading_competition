#!/usr/bin/env python3
"""Build reusable historical monster signal table for fast execution sweeps."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from build_monster_dataset import DEFAULT_HISTORY_MANIFEST, OUT_ROOT, BARS, _load_symbol_data, _market_panels, _relpath, _sample_row  # noqa: E402
from score_monster_watchlist import _score_row, _select_score_features  # noqa: E402

DEFAULT_TRAINING = OUT_ROOT / "monster_samples_clustered_5m_v1" / "samples.parquet"
DEFAULT_FEATURE_SUMMARY = OUT_ROOT / "monster_samples_clustered_5m_v1" / "feature_summary.csv"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build historical monster signal table")
    p.add_argument("--history-manifest", default=str(DEFAULT_HISTORY_MANIFEST))
    p.add_argument("--training-samples", default=str(DEFAULT_TRAINING))
    p.add_argument("--feature-summary", default=str(DEFAULT_FEATURE_SUMMARY))
    p.add_argument("--dataset-id", default="monster_signal_table_v1")
    p.add_argument("--start", default="2026-01-01")
    p.add_argument("--end", default="2026-04-26")
    p.add_argument("--rebalance-minutes", type=int, default=240)
    p.add_argument("--min-score", type=float, default=0.75)
    p.add_argument("--feature-count", type=int, default=25)
    p.add_argument("--timeframe", default="5m")
    p.add_argument("--progress-every", type=int, default=25)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    manifest = json.loads(Path(args.history_manifest).read_text())
    data = _load_symbol_data(manifest["symbols"], args.timeframe)
    close_panel = pd.concat({sym: item.frame["close"] for sym, item in data.items()}, axis=1).sort_index()
    market = _market_panels(close_panel)
    training = pd.read_parquet(args.training_samples)
    feature_summary = pd.read_csv(args.feature_summary)
    score_features = _select_score_features(feature_summary, training, args.feature_count)

    start_ts = max(_as_utc(args.start), close_panel.index.min() + pd.Timedelta(minutes=5 * BARS["7d"]))
    end_ts = _as_utc(args.end) if args.end else close_panel.index.max()
    timeline = close_panel.loc[(close_panel.index >= start_ts) & (close_panel.index <= end_ts)].index
    step = max(1, args.rebalance_minutes // 5)
    decision_times = timeline[::step]
    rows: list[dict[str, Any]] = []
    for i, ts in enumerate(decision_times, start=1):
        for sym, item in data.items():
            row = _sample_row(sym, ts, item, market, require_forward=False)
            if not row or row.get("market_event_flag") != 0:
                continue
            scored = _score_row(row, training, score_features)
            score = float(scored.get("monster_score_adj") or 0.0)
            if score < args.min_score:
                continue
            rows.append(
                {
                    "decision_ts": ts.isoformat(),
                    "symbol": sym,
                    "score": score,
                    "monster_score": scored.get("monster_score"),
                    "ret_1h": row.get("ret_1h"),
                    "ret_6h": row.get("ret_6h"),
                    "ret_24h": row.get("ret_24h"),
                    "rvol_6h": row.get("rvol_6h"),
                    "range_pct_6h": row.get("range_pct_6h"),
                    "market_event_flag": row.get("market_event_flag"),
                    "trigger_reasons": scored.get("trigger_reasons", ""),
                }
            )
        if args.progress_every and i % args.progress_every == 0:
            print(f"decision {i}/{len(decision_times)} ts={ts.isoformat()} rows={len(rows)}", flush=True)

    signals = pd.DataFrame(rows)
    out_dir = OUT_ROOT / args.dataset_id
    out_dir.mkdir(parents=True, exist_ok=True)
    signals.to_csv(out_dir / "signals.csv", index=False)
    if not signals.empty:
        signals.to_parquet(out_dir / "signals.parquet")
    payload = {
        "dataset_id": args.dataset_id,
        "created_at": pd.Timestamp.utcnow().isoformat(),
        "history_manifest": _relpath(Path(args.history_manifest)),
        "training_samples": _relpath(Path(args.training_samples)),
        "feature_summary": _relpath(Path(args.feature_summary)),
        "symbols_loaded": len(data),
        "start": args.start,
        "end": args.end,
        "rebalance_minutes": args.rebalance_minutes,
        "min_score": args.min_score,
        "decision_count": len(decision_times),
        "rows": len(signals),
        "score_features": score_features,
        "artifacts": {"signals": _relpath(out_dir / "signals.csv")},
    }
    (out_dir / "manifest.json").write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0


def _as_utc(value: str) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


if __name__ == "__main__":
    raise SystemExit(main())
