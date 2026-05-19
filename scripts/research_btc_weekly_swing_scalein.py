#!/usr/bin/env python3
"""Research BTC weekly swing strategy with base tranche, add-on tranche, and thesis exits."""

from __future__ import annotations

import argparse
import itertools
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT / "engine" / "data" / "cache"
OUT_ROOT = ROOT / "engine" / "data" / "research" / "btc_weekly_swing_scalein"


@dataclass
class Tranche:
    label: str
    entry_ts: pd.Timestamp
    entry_price: float
    notional: float
    fee_paid: float


@dataclass
class Position:
    entry_level: float
    stop: float
    high_water: float
    tranches: list[Tranche] = field(default_factory=list)
    added: bool = False

    @property
    def entry_ts(self) -> pd.Timestamp:
        return min(t.entry_ts for t in self.tranches)

    @property
    def notional(self) -> float:
        return float(sum(t.notional for t in self.tranches))

    @property
    def avg_entry(self) -> float:
        total = self.notional
        if total <= 0:
            return math.nan
        return float(sum(t.entry_price * t.notional for t in self.tranches) / total)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backtest BTC weekly swing scale-in strategy")
    p.add_argument("--start", default="2020-01-01")
    p.add_argument("--end", default="2026-05-14")
    p.add_argument("--initial-capital", type=float, default=100.0)
    p.add_argument("--lookback-weeks", type=int, default=13)
    p.add_argument("--monthly-sma", type=int, default=6)
    p.add_argument("--weekly-exit-sma", type=int, default=20)
    p.add_argument("--breakout-buffer-pct", type=float, default=0.004)
    p.add_argument("--exit-buffer-pct", type=float, default=0.006)
    p.add_argument("--base-weight", type=float, default=0.45)
    p.add_argument("--base-leverage", type=float, default=1.5)
    p.add_argument("--add-weight", type=float, default=0.25)
    p.add_argument("--add-leverage", type=float, default=2.0)
    p.add_argument("--add-trigger-pct", type=float, default=0.08)
    p.add_argument("--initial-stop-pct", type=float, default=0.12)
    p.add_argument("--trail-stop-pct", type=float, default=0.22)
    p.add_argument("--max-gross-exposure", type=float, default=1.25)
    p.add_argument("--fee-bps-per-side", type=float, default=5.0)
    p.add_argument("--slippage-bps-per-side", type=float, default=2.0)
    p.add_argument("--grid", action="store_true")
    p.add_argument("--out-id", default=None)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.grid:
        rows = []
        for overrides in _grid_overrides():
            run_args = argparse.Namespace(**{**vars(args), **overrides, "grid": False})
            weekly = _weekly_frame(run_args)
            result = _simulate(weekly, run_args)
            summary = _summarize(result["equity"], result["trades"], _buy_hold_curve(weekly, run_args), run_args)
            rows.append({k: v for k, v in summary.items() if k != "args"} | overrides)
        out_dir = OUT_ROOT / (args.out_id or f"btc_weekly_swing_grid_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}")
        out_dir.mkdir(parents=True, exist_ok=True)
        ranked = pd.DataFrame(rows).sort_values(["score"], ascending=False)
        ranked.to_csv(out_dir / "grid_summary.csv", index=False)
        (out_dir / "summary.json").write_text(json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(), "rows": rows}, indent=2, sort_keys=True))
        print(json.dumps({"out_dir": str(out_dir.relative_to(ROOT)), "top": ranked.head(20).to_dict(orient="records")}, indent=2, sort_keys=True))
        return 0

    weekly = _weekly_frame(args)
    result = _simulate(weekly, args)
    buy_hold = _buy_hold_curve(weekly, args)
    summary = _summarize(result["equity"], result["trades"], buy_hold, args)
    out_dir = _write_outputs(result["equity"], result["trades"], buy_hold, summary, args)
    print(json.dumps({"out_dir": str(out_dir.relative_to(ROOT)), "summary": summary}, indent=2, sort_keys=True))
    return 0


def _grid_overrides() -> list[dict[str, Any]]:
    rows = []
    for lookback, base_lev, add_lev, add_trigger, exit_sma, trail in itertools.product(
        [8, 13, 20],
        [1.5, 2.0],
        [1.5, 2.0],
        [0.06, 0.08, 0.12],
        [16, 20, 26],
        [0.18, 0.22, 0.28],
    ):
        rows.append(
            {
                "lookback_weeks": lookback,
                "base_leverage": base_lev,
                "add_leverage": add_lev,
                "add_trigger_pct": add_trigger,
                "weekly_exit_sma": exit_sma,
                "trail_stop_pct": trail,
            }
        )
    return rows


def _weekly_frame(args: argparse.Namespace) -> pd.DataFrame:
    path = CACHE_DIR / "BTC_USDT_futures_1d.parquet"
    if not path.exists():
        raise SystemExit(f"missing cache: {path}")
    daily = pd.read_parquet(path).copy()
    daily.index = pd.to_datetime(daily.index, utc=True)
    daily = daily.sort_index()
    start = pd.Timestamp(args.start, tz="UTC")
    end = pd.Timestamp(args.end, tz="UTC")
    daily = daily.loc[(daily.index >= start) & (daily.index <= end)]
    for col in ("open", "high", "low", "close", "volume"):
        daily[col] = pd.to_numeric(daily[col], errors="coerce")
    daily = daily.dropna(subset=["open", "high", "low", "close"])
    weekly = daily.resample("W-SUN").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
    monthly = daily.resample("ME").agg({"close": "last"}).dropna()
    monthly["monthly_sma"] = monthly["close"].rolling(int(args.monthly_sma), min_periods=max(3, int(args.monthly_sma) // 2)).mean()
    monthly["monthly_sma_slope"] = monthly["monthly_sma"] / monthly["monthly_sma"].shift(1) - 1.0
    weekly = weekly.join(monthly[["close", "monthly_sma", "monthly_sma_slope"]].rename(columns={"close": "monthly_close"}).reindex(weekly.index, method="ffill"))
    weekly["prior_high"] = weekly["high"].rolling(int(args.lookback_weeks), min_periods=max(6, int(args.lookback_weeks) // 2)).max().shift(1)
    weekly["weekly_exit_sma"] = weekly["close"].rolling(int(args.weekly_exit_sma), min_periods=max(5, int(args.weekly_exit_sma) // 2)).mean()
    weekly["monthly_bull"] = (weekly["monthly_close"] > weekly["monthly_sma"]) & (weekly["monthly_sma_slope"] >= -0.01)
    weekly["breakout"] = weekly["monthly_bull"] & (weekly["close"] > weekly["prior_high"] * (1.0 + float(args.breakout_buffer_pct)))
    weekly["thesis_ok"] = weekly["monthly_bull"] & (weekly["close"] >= weekly["weekly_exit_sma"] * (1.0 - float(args.exit_buffer_pct)))
    return weekly.dropna(subset=["prior_high", "weekly_exit_sma", "monthly_sma"])


def _simulate(weekly: pd.DataFrame, args: argparse.Namespace) -> dict[str, pd.DataFrame]:
    nav = float(args.initial_capital)
    pos: Position | None = None
    pending: dict[str, float] | None = None
    equity: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    fee_slip = _fee_slip(args)

    for ts, row in weekly.iterrows():
        if pending is not None and pos is not None:
            px = float(row["open"])
            label = str(pending["label"])
            raw_notional = nav * float(pending["weight"]) * float(pending["leverage"])
            gross_cap = float(args.initial_capital) * float(args.max_gross_exposure)
            notional = max(0.0, min(raw_notional, gross_cap - pos.notional))
            if px > 0 and notional > 0:
                nav -= notional * fee_slip
                pos.tranches.append(Tranche(label=label, entry_ts=ts, entry_price=px, notional=notional, fee_paid=notional * fee_slip))
                if label == "add":
                    pos.added = True
            pending = None

        if pending is not None and pos is None:
            px = float(row["open"])
            raw_notional = nav * float(pending["weight"]) * float(pending["leverage"])
            notional = max(0.0, min(raw_notional, float(args.initial_capital) * float(args.max_gross_exposure)))
            if px > 0 and notional > 0:
                nav -= notional * fee_slip
                level = float(pending["level"])
                stop = min(px * (1.0 - float(args.initial_stop_pct)), level * (1.0 - float(args.exit_buffer_pct)))
                pos = Position(entry_level=level, stop=stop, high_water=px)
                pos.tranches.append(Tranche(label="base", entry_ts=ts, entry_price=px, notional=notional, fee_paid=notional * fee_slip))
            pending = None

        if pos is not None:
            pos.high_water = max(pos.high_water, float(row["high"]))
            pos.stop = max(pos.stop, pos.high_water * (1.0 - float(args.trail_stop_pct)))
            exit_price = None
            exit_reason = ""
            if float(row["low"]) <= pos.stop:
                exit_price = pos.stop
                exit_reason = "stop_or_trail"
            elif not bool(row["thesis_ok"]):
                exit_price = float(row["close"])
                exit_reason = "sma_or_monthly_thesis_lost"
            elif float(row["close"]) < pos.entry_level * (1.0 - float(args.exit_buffer_pct)):
                exit_price = float(row["close"])
                exit_reason = "structure_break"
            if exit_price is not None:
                nav = _close_position(ts, pos, exit_price, exit_reason, nav, trades, fee_slip)
                pos = None

        if pos is not None and not pos.added and pending is None:
            add_ok = (
                bool(row["thesis_ok"])
                and float(row["close"]) >= pos.avg_entry * (1.0 + float(args.add_trigger_pct))
                and float(row["close"]) >= float(row["weekly_exit_sma"])
            )
            if add_ok:
                pending = {"label": "add", "weight": float(args.add_weight), "leverage": float(args.add_leverage)}

        if pos is None and pending is None and bool(row["breakout"]):
            pending = {
                "label": "base",
                "weight": float(args.base_weight),
                "leverage": float(args.base_leverage),
                "level": float(row["prior_high"]),
            }

        mtm_nav = nav
        if pos is not None:
            px = float(row["close"])
            for tr in pos.tranches:
                mtm_nav += tr.notional * (px / tr.entry_price - 1.0 - fee_slip)
        equity.append(
            {
                "ts": ts.isoformat(),
                "nav": mtm_nav,
                "realized_nav": nav,
                "open_positions": 0 if pos is None else 1,
                "tranches": 0 if pos is None else len(pos.tranches),
            }
        )

    if pos is not None:
        final_ts = pd.Timestamp(weekly.index[-1])
        nav = _close_position(final_ts, pos, float(weekly.iloc[-1]["close"]), "forced_end", nav, trades, fee_slip)
        equity.append({"ts": final_ts.isoformat(), "nav": nav, "realized_nav": nav, "open_positions": 0, "tranches": 0})
    return {"equity": pd.DataFrame(equity), "trades": pd.DataFrame(trades)}


def _close_position(
    ts: pd.Timestamp,
    pos: Position,
    exit_price: float,
    reason: str,
    nav: float,
    trades: list[dict[str, Any]],
    fee_slip: float,
) -> float:
    gross_pnl = 0.0
    weighted_net_returns = []
    for tr in pos.tranches:
        gross = float(exit_price) / tr.entry_price - 1.0
        gross_pnl += tr.notional * gross
        weighted_net_returns.append((gross - fee_slip) * tr.notional)
    exit_cost = pos.notional * fee_slip
    net_pnl = gross_pnl - exit_cost
    net_return = float(sum(weighted_net_returns) / pos.notional) if pos.notional > 0 else math.nan
    trades.append(
        {
            "entry_ts": pos.entry_ts.isoformat(),
            "exit_ts": pd.Timestamp(ts).isoformat(),
            "symbol": "BTC/USDT",
            "side": "long",
            "entry_price": pos.avg_entry,
            "exit_price": float(exit_price),
            "notional": pos.notional,
            "tranches": len(pos.tranches),
            "entry_level": pos.entry_level,
            "stop": pos.stop,
            "gross_pnl": gross_pnl,
            "net_pnl": net_pnl,
            "net_return": net_return,
            "exit_reason": reason,
            "hold_weeks": (pd.Timestamp(ts) - pos.entry_ts).days / 7.0,
        }
    )
    return nav + net_pnl


def _fee_slip(args: argparse.Namespace) -> float:
    return (float(args.fee_bps_per_side) + float(args.slippage_bps_per_side)) / 10000.0


def _buy_hold_curve(weekly: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    nav = float(args.initial_capital) * (weekly["close"] / weekly["close"].iloc[0])
    return pd.DataFrame({"ts": nav.index.astype(str), "nav": nav.to_numpy()})


def _summarize(equity: pd.DataFrame, trades: pd.DataFrame, buy_hold: pd.DataFrame, args: argparse.Namespace) -> dict[str, Any]:
    eq = equity.copy()
    eq["ts"] = pd.to_datetime(eq["ts"], utc=True)
    nav = pd.to_numeric(eq["nav"], errors="coerce")
    total_return = float(nav.iloc[-1] / nav.iloc[0] - 1.0)
    days = max(1e-9, (eq["ts"].iloc[-1] - eq["ts"].iloc[0]).total_seconds() / 86400.0)
    ann = (1.0 + total_return) ** (365.0 / days) - 1.0 if total_return > -1 else -1.0
    peak = nav.cummax()
    dd = nav / peak - 1.0
    weekly_ret = nav.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    sharpe = float(weekly_ret.mean() / weekly_ret.std() * math.sqrt(52)) if len(weekly_ret) > 2 and weekly_ret.std() > 0 else math.nan
    bh_nav = pd.to_numeric(buy_hold["nav"], errors="coerce")
    bh_ret = float(bh_nav.iloc[-1] / bh_nav.iloc[0] - 1.0)
    trades_count = int(len(trades))
    if trades.empty:
        win_rate = math.nan
        avg_hold = math.nan
        avg_net = math.nan
        add_rate = math.nan
    else:
        net_pnl = pd.to_numeric(trades["net_pnl"], errors="coerce")
        win_rate = float((net_pnl > 0).mean())
        avg_hold = float(pd.to_numeric(trades["hold_weeks"], errors="coerce").mean())
        avg_net = float(pd.to_numeric(trades["net_return"], errors="coerce").mean())
        add_rate = float((pd.to_numeric(trades["tranches"], errors="coerce") > 1).mean())
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
        "trades": trades_count,
        "win_rate": win_rate,
        "avg_net_return": avg_net,
        "avg_hold_weeks": avg_hold,
        "add_rate": add_rate,
        "score": score,
        "args": vars(args),
    }


def _write_outputs(equity: pd.DataFrame, trades: pd.DataFrame, buy_hold: pd.DataFrame, summary: dict[str, Any], args: argparse.Namespace) -> Path:
    out_dir = OUT_ROOT / (args.out_id or f"btc_weekly_swing_scalein_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}")
    out_dir.mkdir(parents=True, exist_ok=True)
    equity.to_csv(out_dir / "equity_curve.csv", index=False)
    buy_hold.to_csv(out_dir / "buy_hold_curve.csv", index=False)
    trades.to_csv(out_dir / "trades.csv", index=False)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
    return out_dir


if __name__ == "__main__":
    raise SystemExit(main())
