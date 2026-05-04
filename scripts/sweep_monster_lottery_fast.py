#!/usr/bin/env python3
"""Fast in-process parameter sweep for monster lottery backtests."""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import backtest_monster_lottery as bt  # noqa: E402
from build_monster_dataset import DEFAULT_HISTORY_MANIFEST, OUT_ROOT, BARS, _load_symbol_data, _market_panels  # noqa: E402
from score_monster_watchlist import _select_score_features  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Fast sweep monster lottery parameters")
    p.add_argument("--sweep-id", default=None)
    p.add_argument("--start", default="2026-01-01")
    p.add_argument("--end", default="2026-04-26")
    p.add_argument("--rebalance-minutes", type=int, default=240)
    p.add_argument("--risk-budgets", default="10,20")
    p.add_argument("--long-scores", default="0.88,0.90,0.92,0.95")
    p.add_argument("--stop-losses", default="0.08,0.10,0.15")
    p.add_argument("--tp-packs", default="0.30:0.80:0.25,0.50:1.50:0.30")
    p.add_argument("--max-runs", type=int, default=0)
    p.add_argument("--timeframe", default="5m")
    p.add_argument("--history-manifest", default=str(DEFAULT_HISTORY_MANIFEST))
    p.add_argument("--training-samples", default=str(bt.DEFAULT_TRAINING))
    p.add_argument("--feature-summary", default=str(bt.DEFAULT_FEATURE_SUMMARY))
    p.add_argument("--progress", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    sweep_id = args.sweep_id or f"monster_lottery_fast_sweep_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    out_dir = OUT_ROOT / sweep_id
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest = json.loads(Path(args.history_manifest).read_text())
    data = _load_symbol_data(manifest["symbols"], args.timeframe)
    close_panel = pd.concat({sym: item.frame["close"] for sym, item in data.items()}, axis=1).sort_index()
    market = _market_panels(close_panel)
    training = pd.read_parquet(args.training_samples)
    feature_summary = pd.read_csv(args.feature_summary)
    score_features = _select_score_features(feature_summary, training, 25)
    start_ts = max(bt._as_utc(args.start), close_panel.index.min() + pd.Timedelta(minutes=5 * BARS["7d"]))
    end_ts = bt._as_utc(args.end) if args.end else close_panel.index.max()
    timeline = close_panel.loc[(close_panel.index >= start_ts) & (close_panel.index <= end_ts)].index

    configs = _configs(args)
    if args.max_runs:
        configs = configs[: args.max_runs]

    rows = []
    for i, cfg in enumerate(configs, start=1):
        run_args = _run_args(args, cfg)
        state = bt._run(run_args, data, close_panel, market, training, score_features, timeline)
        trades = pd.DataFrame(state["trades"])
        equity = pd.DataFrame(state["equity"])
        metrics = bt._metrics(equity, trades, run_args)
        row = {"run_index": i, **cfg, **{f"metric_{k}": v for k, v in metrics.items()}}
        row["score_objective"] = _objective(metrics)
        rows.append(row)
        if args.progress:
            print(f"[{i}/{len(configs)}] objective={row['score_objective']:.4f} nav={metrics.get('final_nav'):.2f} cfg={cfg}", flush=True)

    df = pd.DataFrame(rows).sort_values("score_objective", ascending=False, na_position="last")
    df.to_csv(out_dir / "results.csv", index=False)
    df.to_parquet(out_dir / "results.parquet")
    payload = {
        "sweep_id": sweep_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "start": args.start,
        "end": args.end,
        "runs": len(rows),
        "symbols_loaded": len(data),
        "timeline_start": timeline[0].isoformat() if len(timeline) else None,
        "timeline_end": timeline[-1].isoformat() if len(timeline) else None,
        "artifacts": {"results_csv": str((out_dir / "results.csv").relative_to(ROOT))},
        "top": df.head(20).to_dict(orient="records"),
    }
    (out_dir / "manifest.json").write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0


def _run_args(args: argparse.Namespace, cfg: dict[str, Any]) -> argparse.Namespace:
    return argparse.Namespace(
        initial_capital=1000.0,
        risk_budget=float(cfg["risk_budget"]),
        max_open_risk=60.0,
        max_positions=3,
        leverage=5.0,
        stop_loss=float(cfg["stop_loss"]),
        tp1=float(cfg["tp1"]),
        tp1_fraction=0.35,
        tp2=float(cfg["tp2"]),
        tp2_fraction=0.35,
        runner_trailing=float(cfg["runner_trailing"]),
        max_hold_hours=120.0,
        long_score=float(cfg["long_score"]),
        short_score=0.88,
        short_pump_24h=0.60,
        short_break_1h=-0.08,
        max_long_ret_1h=0.30,
        cooldown_hours=24.0,
        fee_bps_per_side=4.0,
        slippage_bps_per_side=4.0,
        rebalance_minutes=args.rebalance_minutes,
        progress_every=0,
    )


def _configs(args: argparse.Namespace) -> list[dict[str, Any]]:
    risks = [float(x) for x in args.risk_budgets.split(",") if x]
    scores = [float(x) for x in args.long_scores.split(",") if x]
    stops = [float(x) for x in args.stop_losses.split(",") if x]
    packs = []
    for raw in args.tp_packs.split(","):
        tp1, tp2, trail = [float(x) for x in raw.split(":")]
        packs.append({"tp1": tp1, "tp2": tp2, "runner_trailing": trail})
    return [{"risk_budget": r, "long_score": s, "stop_loss": st, **pack} for r, s, st, pack in itertools.product(risks, scores, stops, packs)]


def _objective(metrics: dict[str, Any]) -> float:
    total_return = float(metrics.get("total_return") or 0.0)
    max_dd = abs(float(metrics.get("max_drawdown") or 0.0))
    best_r = float(metrics.get("best_return_on_risk_budget") or 0.0)
    worst_r = abs(float(metrics.get("worst_return_on_risk_budget") or 0.0))
    profit_factor = float(metrics.get("profit_factor") or 0.0)
    return total_return * 2.0 + best_r * 0.15 + profit_factor * 0.25 - max_dd * 1.5 - worst_r * 0.15


if __name__ == "__main__":
    raise SystemExit(main())
