#!/usr/bin/env python3
"""Research simple tradable alphas for OKX stock-like tokens."""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from research_okx_stock_token_tracking import _load_okx, _load_yfinance


ROOT = Path(__file__).resolve().parents[1]
OUT_ROOT = ROOT / "engine" / "data" / "research" / "okx_stock_token_alpha"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backtest simple OKX stock token alpha ideas")
    p.add_argument("--symbols", default="AMD,AMZN,COIN,GOOGL,HOOD,INTC,MSTR,NVDA,TSLA")
    p.add_argument("--start", default="2026-02-01")
    p.add_argument("--end", default="2026-05-14")
    p.add_argument("--threshold", type=float, default=0.015)
    p.add_argument("--fee-bps-per-side", type=float, default=5.0)
    p.add_argument("--slippage-bps-per-side", type=float, default=2.0)
    p.add_argument("--out-id", default=None)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    tickers = [s.strip().upper() for s in str(args.symbols).split(",") if s.strip()]
    rows: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    for ticker in tickers:
        okx = _load_okx(ticker, args)
        equity = _load_yfinance(ticker, args)
        joined = okx.join(equity, how="inner").dropna()
        if len(joined) < 25:
            rows.append({"ticker": ticker, "status": "too_few_overlap", "days": len(joined)})
            continue
        joined["okx_ret"] = joined["okx_close"].pct_change()
        joined["equity_ret"] = joined["equity_close"].pct_change()
        joined["next_okx_ret"] = joined["okx_ret"].shift(-1)
        joined["dislocation"] = joined["okx_ret"] - joined["equity_ret"]
        for strategy, signal in _signals(joined, args).items():
            sample = joined.assign(signal=signal).dropna(subset=["signal", "next_okx_ret"])
            sample = sample[sample["signal"] != 0]
            result = _evaluate(ticker, strategy, sample, args)
            rows.append(result)
            for ts, row in sample.iterrows():
                trades.append(
                    {
                        "ticker": ticker,
                        "strategy": strategy,
                        "signal_ts": ts.strftime("%Y-%m-%d"),
                        "side": "long" if float(row["signal"]) > 0 else "short",
                        "signal": float(row["signal"]),
                        "next_okx_ret": float(row["next_okx_ret"]),
                        "net_return": float(row["signal"]) * float(row["next_okx_ret"]) - _cost(args),
                    }
                )
    out_dir = OUT_ROOT / (args.out_id or f"stock_token_alpha_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}")
    out_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(out_dir / "alpha_summary.csv", index=False)
    pd.DataFrame(trades).to_csv(out_dir / "trades.csv", index=False)
    payload = {"generated_at": datetime.now(timezone.utc).isoformat(), "args": vars(args), "summary": rows}
    (out_dir / "summary.json").write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(json.dumps({"out_dir": str(out_dir.relative_to(ROOT)), "summary": rows}, indent=2, sort_keys=True))
    return 0


def _signals(df: pd.DataFrame, args: argparse.Namespace) -> dict[str, pd.Series]:
    th = float(args.threshold)
    equity_momo = pd.Series(0.0, index=df.index)
    equity_momo.loc[df["equity_ret"] > th] = 1.0
    equity_momo.loc[df["equity_ret"] < -th] = -1.0

    disloc_revert = pd.Series(0.0, index=df.index)
    disloc_revert.loc[df["dislocation"] > th] = -1.0
    disloc_revert.loc[df["dislocation"] < -th] = 1.0

    okx_momo = pd.Series(0.0, index=df.index)
    okx_momo.loc[df["okx_ret"] > th] = 1.0
    okx_momo.loc[df["okx_ret"] < -th] = -1.0
    return {
        "equity_prevday_momentum": equity_momo,
        "dislocation_reversion": disloc_revert,
        "okx_prevday_momentum": okx_momo,
    }


def _cost(args: argparse.Namespace) -> float:
    return 2.0 * (float(args.fee_bps_per_side) + float(args.slippage_bps_per_side)) / 10000.0


def _evaluate(ticker: str, strategy: str, sample: pd.DataFrame, args: argparse.Namespace) -> dict[str, Any]:
    if sample.empty:
        return {"ticker": ticker, "strategy": strategy, "status": "empty", "trades": 0}
    net = sample["signal"] * sample["next_okx_ret"] - _cost(args)
    return {
        "ticker": ticker,
        "strategy": strategy,
        "status": "ok",
        "trades": int(len(net)),
        "win_rate": float((net > 0).mean()),
        "avg_net_return": float(net.mean()),
        "median_net_return": float(net.median()),
        "sum_net_return": float(net.sum()),
        "sharpe_like": float(net.mean() / net.std() * math.sqrt(252)) if len(net) > 2 and float(net.std()) > 0 else math.nan,
    }


if __name__ == "__main__":
    raise SystemExit(main())
