#!/usr/bin/env python3
"""Evaluate rule overlays on OKX stock-token module backtests."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import research_okx_stock_token_modules as base  # noqa: E402


OUT_ROOT = ROOT / "engine" / "data" / "research" / "okx_stock_token_rule_overlays"
NY = ZoneInfo("America/New_York")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Evaluate stock-token rule overlays")
    p.add_argument("--symbols", required=True)
    p.add_argument("--module", required=True, choices=["okx_momentum_capped", "legacy_equity_momentum", "equity_momentum_okx_confirmed"])
    p.add_argument("--start", default="2026-02-01")
    p.add_argument("--end", default="2026-05-15")
    p.add_argument("--threshold", type=float, default=0.02)
    p.add_argument("--stop-pct", type=float, default=0.03)
    p.add_argument("--target-pct", type=float, default=0.05)
    p.add_argument("--max-hold-hours", type=float, default=48.0)
    p.add_argument("--cooldown-hours", type=float, default=12.0)
    p.add_argument("--overheat-pct", type=float, default=0.06)
    p.add_argument("--equity-confirm-pct", type=float, default=0.0)
    p.add_argument("--daily-loss-usdt", type=float, default=0.0)
    p.add_argument("--notional-usdt", type=float, default=10.0)
    p.add_argument("--out-id", default=None)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    tickers = [item.strip().upper() for item in str(args.symbols).split(",") if item.strip()]
    params = base.Params(
        threshold=float(args.threshold),
        stop_pct=float(args.stop_pct),
        target_pct=float(args.target_pct),
        max_hold_hours=float(args.max_hold_hours),
        cooldown_hours=float(args.cooldown_hours),
        overheat_pct=float(args.overheat_pct),
    )
    raw_trades: list[dict[str, Any]] = []
    for ticker in tickers:
        okx_daily = base.load_okx_daily(ticker, args.start, args.end)
        okx_5m = base.load_okx_5m(ticker, args.start, args.end)
        equity = base.load_equity_daily(ticker, args.start, args.end)
        if okx_daily.empty or okx_5m.empty or equity.empty:
            continue
        joined = okx_daily.join(equity, how="inner").dropna()
        if len(joined) < 8:
            continue
        joined["okx_ret1"] = joined["okx_close"].pct_change()
        joined["equity_ret1"] = joined["equity_close"].pct_change()
        joined["dislocation"] = joined["okx_ret1"] - joined["equity_ret1"]
        modules = base.build_signals(joined, params)
        raw_trades.extend(base.backtest_module(ticker, args.module, modules[args.module], okx_5m, params))

    filtered, rejects = apply_overlays(raw_trades, args)
    summary = summarize(filtered, args)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "args": vars(args),
        "raw_summary": summarize(raw_trades, args),
        "overlay_summary": summary,
        "rejects": rejects,
    }
    out_dir = OUT_ROOT / (args.out_id or f"overlay_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}")
    out_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(raw_trades).to_csv(out_dir / "raw_trades.csv", index=False)
    pd.DataFrame(filtered).to_csv(out_dir / "overlay_trades.csv", index=False)
    (out_dir / "summary.json").write_text(json.dumps(report, indent=2, sort_keys=True))
    print(json.dumps({"out_dir": str(out_dir.relative_to(ROOT)), **report}, indent=2, sort_keys=True))
    return 0


def apply_overlays(trades: list[dict[str, Any]], args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, int]]:
    rejects: dict[str, int] = defaultdict(int)
    accepted: list[dict[str, Any]] = []
    day_pnl: dict[str, float] = defaultdict(float)
    for trade in sorted(trades, key=lambda item: str(item.get("entry_ts") or "")):
        if float(args.equity_confirm_pct) > 0 and str(args.module) == "okx_momentum_capped":
            side = str(trade.get("side") or "")
            eq = _float(trade.get("equity_ret1"))
            floor = abs(float(args.equity_confirm_pct))
            if side == "long" and not (eq is not None and eq >= floor):
                rejects["equity_confirm"] += 1
                continue
            if side == "short" and not (eq is not None and eq <= -floor):
                rejects["equity_confirm"] += 1
                continue
        day = _ny_day(str(trade.get("entry_ts") or ""))
        if float(args.daily_loss_usdt) > 0 and day_pnl[day] <= -abs(float(args.daily_loss_usdt)):
            rejects["daily_loss_fuse"] += 1
            continue
        accepted.append(trade)
        net = _float(trade.get("net_return")) or 0.0
        day_pnl[day] += net * float(args.notional_usdt)
    return accepted, dict(rejects)


def summarize(trades: list[dict[str, Any]], args: argparse.Namespace) -> dict[str, Any]:
    if not trades:
        return {
            "trades": 0,
            "win_rate": None,
            "sum_net_return": 0.0,
            "pnl_usdt": 0.0,
            "sharpe_like": None,
            "hard_stop_rate": None,
            "target_rate": None,
        }
    df = pd.DataFrame(trades)
    ret = pd.to_numeric(df["net_return"], errors="coerce").fillna(0.0)
    std = float(ret.std())
    return {
        "trades": int(len(df)),
        "win_rate": float((ret > 0).mean()),
        "sum_net_return": float(ret.sum()),
        "pnl_usdt": float(ret.sum() * float(args.notional_usdt)),
        "avg_net_return": float(ret.mean()),
        "median_net_return": float(ret.median()),
        "sharpe_like": float(ret.mean() / std * math.sqrt(252)) if len(ret) > 2 and std > 0 else None,
        "hard_stop_rate": float(df["exit_reason"].isin(["hard_stop", "stop_first_conservative"]).mean()),
        "protected_stop_rate": float((df["exit_reason"] == "protected_stop").mean()),
        "target_rate": float((df["exit_reason"] == "target").mean()),
    }


def _float(value: Any) -> float | None:
    try:
        out = float(value)
    except Exception:
        return None
    return out if math.isfinite(out) else None


def _ny_day(value: str) -> str:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert(NY).strftime("%Y-%m-%d")


if __name__ == "__main__":
    raise SystemExit(main())
