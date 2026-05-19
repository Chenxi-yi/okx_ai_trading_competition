#!/usr/bin/env python3
"""Research BTC medium-term swing entries with weekly regime and daily execution."""

from __future__ import annotations

import argparse
import itertools
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT / "engine" / "data" / "cache"
OUT_ROOT = ROOT / "engine" / "data" / "research" / "btc_daily_swing"


@dataclass
class Position:
    entry_ts: pd.Timestamp
    entry_price: float
    notional: float
    stop: float
    high_water: float
    entry_mode: str


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backtest BTC daily swing entries under weekly trend regime")
    p.add_argument("--start", default="2023-01-01")
    p.add_argument("--end", default="2026-05-15")
    p.add_argument("--initial-capital", type=float, default=100.0)
    p.add_argument("--entry-mode", choices=["breakout", "pullback_reclaim", "hybrid"], default="hybrid")
    p.add_argument("--lookback-days", type=int, default=55)
    p.add_argument("--weekly-sma", type=int, default=20)
    p.add_argument("--daily-fast-sma", type=int, default=20)
    p.add_argument("--daily-slow-sma", type=int, default=50)
    p.add_argument("--exit-sma", type=int, default=50)
    p.add_argument("--atr-window", type=int, default=14)
    p.add_argument("--atr-stop-mult", type=float, default=2.5)
    p.add_argument("--trail-stop-pct", type=float, default=0.16)
    p.add_argument("--pullback-near-pct", type=float, default=0.025)
    p.add_argument("--weight", type=float, default=0.5)
    p.add_argument("--leverage", type=float, default=1.5)
    p.add_argument("--max-hold-days", type=int, default=160)
    p.add_argument("--cooldown-days", type=int, default=7)
    p.add_argument("--fee-bps-per-side", type=float, default=5.0)
    p.add_argument("--slippage-bps-per-side", type=float, default=2.0)
    p.add_argument("--grid", action="store_true")
    p.add_argument("--out-id", default=None)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.grid:
        rows = []
        for overrides in _grid():
            run_args = argparse.Namespace(**{**vars(args), **overrides, "grid": False})
            daily = _daily_frame(run_args)
            result = _simulate(daily, run_args)
            summary = _summarize(result["equity"], result["trades"], _buy_hold(daily, run_args), run_args)
            rows.append({k: v for k, v in summary.items() if k != "args"} | overrides)
        out_dir = OUT_ROOT / (args.out_id or f"btc_daily_swing_grid_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}")
        out_dir.mkdir(parents=True, exist_ok=True)
        ranked = pd.DataFrame(rows).sort_values(["score"], ascending=False)
        ranked.to_csv(out_dir / "grid_summary.csv", index=False)
        (out_dir / "summary.json").write_text(json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(), "rows": rows}, indent=2, sort_keys=True))
        print(json.dumps({"out_dir": str(out_dir.relative_to(ROOT)), "top": ranked.head(25).to_dict(orient="records")}, indent=2, sort_keys=True))
        return 0

    daily = _daily_frame(args)
    result = _simulate(daily, args)
    buy_hold = _buy_hold(daily, args)
    summary = _summarize(result["equity"], result["trades"], buy_hold, args)
    out_dir = _write(result["equity"], result["trades"], buy_hold, summary, args)
    print(json.dumps({"out_dir": str(out_dir.relative_to(ROOT)), "summary": summary}, indent=2, sort_keys=True))
    return 0


def _grid() -> list[dict[str, Any]]:
    rows = []
    for entry_mode, lookback, leverage, exit_sma, atr_mult, trail, max_hold in itertools.product(
        ["breakout", "pullback_reclaim", "hybrid"],
        [20, 40, 55, 80],
        [1.0, 1.5, 2.0],
        [20, 50, 100],
        [2.0, 2.5, 3.0],
        [0.10, 0.14, 0.18],
        [60, 120, 220],
    ):
        rows.append(
            {
                "entry_mode": entry_mode,
                "lookback_days": lookback,
                "leverage": leverage,
                "exit_sma": exit_sma,
                "atr_stop_mult": atr_mult,
                "trail_stop_pct": trail,
                "max_hold_days": max_hold,
            }
        )
    return rows


def _daily_frame(args: argparse.Namespace) -> pd.DataFrame:
    path = CACHE_DIR / "BTC_USDT_futures_1d.parquet"
    if not path.exists():
        raise SystemExit(f"missing cache: {path}")
    df = pd.read_parquet(path).copy()
    df.index = pd.to_datetime(df.index, utc=True)
    df = df.sort_index()
    start = pd.Timestamp(args.start, tz="UTC")
    end = pd.Timestamp(args.end, tz="UTC")
    df = df.loc[(df.index >= start) & (df.index <= end)]
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"])
    weekly = df.resample("W-SUN").agg({"close": "last"}).dropna()
    weekly["weekly_sma"] = weekly["close"].rolling(int(args.weekly_sma), min_periods=max(5, int(args.weekly_sma) // 2)).mean()
    weekly["weekly_slope"] = weekly["weekly_sma"] / weekly["weekly_sma"].shift(1) - 1.0
    df = df.join(weekly[["close", "weekly_sma", "weekly_slope"]].rename(columns={"close": "weekly_close"}).reindex(df.index, method="ffill"))
    df["fast_sma"] = df["close"].rolling(int(args.daily_fast_sma), min_periods=max(5, int(args.daily_fast_sma) // 2)).mean()
    df["slow_sma"] = df["close"].rolling(int(args.daily_slow_sma), min_periods=max(10, int(args.daily_slow_sma) // 2)).mean()
    df["exit_sma"] = df["close"].rolling(int(args.exit_sma), min_periods=max(5, int(args.exit_sma) // 2)).mean()
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    df["atr"] = tr.rolling(int(args.atr_window), min_periods=max(5, int(args.atr_window) // 2)).mean()
    df["prior_high"] = df["high"].rolling(int(args.lookback_days), min_periods=max(10, int(args.lookback_days) // 2)).max().shift(1)
    df["regime_bull"] = (df["weekly_close"] > df["weekly_sma"]) & (df["weekly_slope"] >= -0.005)
    df["breakout_signal"] = df["regime_bull"] & (df["close"] > df["prior_high"])
    df["pullback_signal"] = (
        df["regime_bull"]
        & (df["low"].rolling(5, min_periods=2).min() <= df["fast_sma"] * (1.0 + float(args.pullback_near_pct)))
        & (df["close"] > df["fast_sma"])
        & (df["close"] > df["close"].shift(1))
        & (df["close"] > df["slow_sma"])
    )
    return df.dropna(subset=["weekly_sma", "fast_sma", "slow_sma", "exit_sma", "atr", "prior_high"])


def _simulate(df: pd.DataFrame, args: argparse.Namespace) -> dict[str, pd.DataFrame]:
    nav = float(args.initial_capital)
    pos: Position | None = None
    pending_mode: str | None = None
    cooldown_until = pd.Timestamp.min.tz_localize("UTC")
    fee_slip = _fee_slip(args)
    equity = []
    trades = []

    for ts, row in df.iterrows():
        if pending_mode and pos is None:
            entry = float(row["open"])
            notional = nav * float(args.weight) * float(args.leverage)
            if entry > 0 and notional > 0:
                nav -= notional * fee_slip
                stop = entry - float(row["atr"]) * float(args.atr_stop_mult)
                pos = Position(ts, entry, notional, stop, entry, pending_mode)
            pending_mode = None

        if pos is not None:
            pos.high_water = max(pos.high_water, float(row["high"]))
            pos.stop = max(pos.stop, pos.high_water * (1.0 - float(args.trail_stop_pct)))
            hold_days = (pd.Timestamp(ts) - pos.entry_ts).days
            exit_price = None
            reason = ""
            if float(row["low"]) <= pos.stop:
                exit_price = pos.stop
                reason = "stop_or_trail"
            elif not bool(row["regime_bull"]) or float(row["close"]) < float(row["exit_sma"]):
                exit_price = float(row["close"])
                reason = "thesis_exit"
            elif hold_days >= int(args.max_hold_days):
                exit_price = float(row["close"])
                reason = "max_hold"
            if exit_price is not None:
                nav, net_pnl = _close(ts, pos, exit_price, reason, nav, trades, fee_slip)
                if net_pnl < 0:
                    cooldown_until = pd.Timestamp(ts) + pd.Timedelta(days=int(args.cooldown_days))
                pos = None

        if pos is None and pending_mode is None and pd.Timestamp(ts) >= cooldown_until:
            breakout = bool(row["breakout_signal"])
            pullback = bool(row["pullback_signal"])
            if str(args.entry_mode) == "breakout" and breakout:
                pending_mode = "breakout"
            elif str(args.entry_mode) == "pullback_reclaim" and pullback:
                pending_mode = "pullback_reclaim"
            elif str(args.entry_mode) == "hybrid" and (breakout or pullback):
                pending_mode = "breakout" if breakout else "pullback_reclaim"

        mtm = nav
        if pos is not None:
            mtm += pos.notional * (float(row["close"]) / pos.entry_price - 1.0 - fee_slip)
        equity.append({"ts": ts.isoformat(), "nav": mtm, "realized_nav": nav, "open_positions": int(pos is not None)})

    if pos is not None:
        ts = pd.Timestamp(df.index[-1])
        nav, _ = _close(ts, pos, float(df.iloc[-1]["close"]), "forced_end", nav, trades, fee_slip)
        equity.append({"ts": ts.isoformat(), "nav": nav, "realized_nav": nav, "open_positions": 0})
    return {"equity": pd.DataFrame(equity), "trades": pd.DataFrame(trades)}


def _close(ts: pd.Timestamp, pos: Position, exit_price: float, reason: str, nav: float, trades: list[dict[str, Any]], fee_slip: float) -> tuple[float, float]:
    gross = float(exit_price) / pos.entry_price - 1.0
    net = gross - fee_slip
    net_pnl = pos.notional * net
    trades.append(
        {
            "entry_ts": pos.entry_ts.isoformat(),
            "exit_ts": pd.Timestamp(ts).isoformat(),
            "symbol": "BTC/USDT",
            "entry_mode": pos.entry_mode,
            "entry_price": pos.entry_price,
            "exit_price": float(exit_price),
            "notional": pos.notional,
            "net_return": net,
            "net_pnl": net_pnl,
            "exit_reason": reason,
            "hold_days": (pd.Timestamp(ts) - pos.entry_ts).days,
        }
    )
    return nav + net_pnl, net_pnl


def _fee_slip(args: argparse.Namespace) -> float:
    return (float(args.fee_bps_per_side) + float(args.slippage_bps_per_side)) / 10000.0


def _buy_hold(df: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    nav = float(args.initial_capital) * df["close"] / df["close"].iloc[0]
    return pd.DataFrame({"ts": nav.index.astype(str), "nav": nav.to_numpy()})


def _summarize(equity: pd.DataFrame, trades: pd.DataFrame, buy_hold: pd.DataFrame, args: argparse.Namespace) -> dict[str, Any]:
    eq = equity.copy()
    eq["ts"] = pd.to_datetime(eq["ts"], utc=True)
    nav = pd.to_numeric(eq["nav"], errors="coerce")
    total_return = float(nav.iloc[-1] / nav.iloc[0] - 1.0)
    days = max(1e-9, (eq["ts"].iloc[-1] - eq["ts"].iloc[0]).total_seconds() / 86400.0)
    ann = (1.0 + total_return) ** (365.0 / days) - 1.0 if total_return > -1 else -1.0
    dd = nav / nav.cummax() - 1.0
    daily_ret = nav.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    sharpe = float(daily_ret.mean() / daily_ret.std() * math.sqrt(365)) if len(daily_ret) > 2 and daily_ret.std() > 0 else math.nan
    bh_nav = pd.to_numeric(buy_hold["nav"], errors="coerce")
    bh_ret = float(bh_nav.iloc[-1] / bh_nav.iloc[0] - 1.0)
    if trades.empty:
        win_rate = math.nan
        avg_net = math.nan
        avg_hold = math.nan
    else:
        net_pnl = pd.to_numeric(trades["net_pnl"], errors="coerce")
        win_rate = float((net_pnl > 0).mean())
        avg_net = float(pd.to_numeric(trades["net_return"], errors="coerce").mean())
        avg_hold = float(pd.to_numeric(trades["hold_days"], errors="coerce").mean())
    score = float(ann / max(0.05, abs(float(dd.min()))))
    return {
        "start": eq["ts"].iloc[0].isoformat(),
        "end": eq["ts"].iloc[-1].isoformat(),
        "initial_nav": float(nav.iloc[0]),
        "final_nav": float(nav.iloc[-1]),
        "total_return": total_return,
        "annualized_return": float(ann),
        "max_drawdown": float(dd.min()),
        "sharpe_like": sharpe,
        "buy_hold_total_return": bh_ret,
        "trades": int(len(trades)),
        "win_rate": win_rate,
        "avg_net_return": avg_net,
        "avg_hold_days": avg_hold,
        "score": score,
        "args": vars(args),
    }


def _write(equity: pd.DataFrame, trades: pd.DataFrame, buy_hold: pd.DataFrame, summary: dict[str, Any], args: argparse.Namespace) -> Path:
    out_dir = OUT_ROOT / (args.out_id or f"btc_daily_swing_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}")
    out_dir.mkdir(parents=True, exist_ok=True)
    equity.to_csv(out_dir / "equity_curve.csv", index=False)
    buy_hold.to_csv(out_dir / "buy_hold_curve.csv", index=False)
    trades.to_csv(out_dir / "trades.csv", index=False)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
    return out_dir


if __name__ == "__main__":
    raise SystemExit(main())
