#!/usr/bin/env python3
"""Analyze monster strategy backtest outputs by regime slices."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OUT_ROOT = ROOT / "engine" / "data" / "monster_events"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Analyze monster backtest trade/equity outputs")
    p.add_argument("--backtest-id", default="monster_backtest_5m_v1")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    run_dir = OUT_ROOT / args.backtest_id
    trades = _read(run_dir / "trades.parquet", run_dir / "trades.csv")
    equity = _read(run_dir / "equity_curve.parquet", run_dir / "equity_curve.csv")
    signals = _read(run_dir / "signals.parquet", run_dir / "signals.csv")
    if trades.empty:
        raise SystemExit(f"No trades found for {args.backtest_id}")

    trades["entry_ts"] = pd.to_datetime(trades["entry_ts"], utc=True)
    trades["exit_ts"] = pd.to_datetime(trades["exit_ts"], utc=True)
    trades["entry_month"] = trades["entry_ts"].dt.to_period("M").astype(str)
    trades["score_bucket"] = pd.cut(
        trades["entry_score"],
        bins=[0.0, 0.75, 0.80, 0.85, 0.90, 0.95, 1.01],
        labels=["<0.75", "0.75-0.80", "0.80-0.85", "0.85-0.90", "0.90-0.95", "0.95+"],
        include_lowest=True,
    )

    equity["ts"] = pd.to_datetime(equity["ts"], utc=True)
    equity["month"] = equity["ts"].dt.to_period("M").astype(str)
    monthly_nav = equity.groupby("month")["nav"].agg(["first", "last", "min", "max"]).reset_index()
    monthly_nav["return"] = monthly_nav["last"] / monthly_nav["first"] - 1.0
    monthly_nav["drawdown_from_month_high"] = monthly_nav["min"] / monthly_nav["max"] - 1.0

    reports = {
        "monthly_trades": _trade_group(trades, "entry_month"),
        "symbol": _trade_group(trades, "symbol").sort_values("pnl_sum", ascending=True),
        "score_bucket": _trade_group(trades, "score_bucket"),
        "exit_reason": _trade_group(trades, "exit_reason"),
        "monthly_nav": monthly_nav,
    }
    if not signals.empty:
        signals["decision_ts"] = pd.to_datetime(signals["decision_ts"], utc=True)
        signals["month"] = signals["decision_ts"].dt.to_period("M").astype(str)
        reports["signal_month"] = signals.groupby("month").agg(
            signals=("symbol", "count"),
            avg_score=("monster_score_adj", "mean"),
            p90_score=("monster_score_adj", lambda x: x.quantile(0.9)),
        ).reset_index()

    for name, df in reports.items():
        df.to_csv(run_dir / f"analysis_{name}.csv", index=False)

    summary = {
        "backtest_id": args.backtest_id,
        "worst_months": reports["monthly_nav"].sort_values("return").head(6).to_dict(orient="records"),
        "best_months": reports["monthly_nav"].sort_values("return", ascending=False).head(6).to_dict(orient="records"),
        "worst_symbols": reports["symbol"].head(12).to_dict(orient="records"),
        "best_symbols": reports["symbol"].tail(12).sort_values("pnl_sum", ascending=False).to_dict(orient="records"),
        "score_buckets": reports["score_bucket"].to_dict(orient="records"),
        "exit_reasons": reports["exit_reason"].to_dict(orient="records"),
        "artifacts": {name: str((run_dir / f"analysis_{name}.csv").relative_to(ROOT)) for name in reports},
    }
    (run_dir / "analysis_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True, default=str))
    print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    return 0


def _read(parquet_path: Path, csv_path: Path) -> pd.DataFrame:
    if parquet_path.exists():
        return pd.read_parquet(parquet_path)
    if csv_path.exists():
        return pd.read_csv(csv_path)
    return pd.DataFrame()


def _trade_group(trades: pd.DataFrame, col: str) -> pd.DataFrame:
    out = trades.groupby(col, observed=True).agg(
        trades=("pnl", "count"),
        pnl_sum=("pnl", "sum"),
        pnl_mean=("pnl", "mean"),
        ret_mean=("return", "mean"),
        ret_median=("return", "median"),
        win_rate=("pnl", lambda x: float((x > 0).mean())),
        avg_score=("entry_score", "mean"),
        avg_hold_hours=("hold_hours", "mean"),
    )
    return out.reset_index().sort_values(col)


if __name__ == "__main__":
    raise SystemExit(main())
