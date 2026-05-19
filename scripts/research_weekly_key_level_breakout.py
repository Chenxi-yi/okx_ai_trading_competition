#!/usr/bin/env python3
"""Research slow weekly key-level breakout strategies for large-cap swaps."""

from __future__ import annotations

import argparse
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
OUT_ROOT = ROOT / "engine" / "data" / "research" / "weekly_key_level_breakout"


@dataclass
class Position:
    symbol: str
    side: str
    entry_ts: pd.Timestamp
    entry_price: float
    notional: float
    level: float
    stop: float
    target: float
    high_water: float
    low_water: float


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Research weekly/monthly key-level breakout strategy")
    p.add_argument("--symbols", default="BTC_USDT,ETH_USDT,SOL_USDT")
    p.add_argument("--start", default="2023-01-01")
    p.add_argument("--end", default="2026-05-14")
    p.add_argument("--initial-capital", type=float, default=100.0)
    p.add_argument("--lookback-weeks", type=int, default=26)
    p.add_argument("--monthly-sma", type=int, default=6)
    p.add_argument("--entry-mode", choices=["breakout", "breakout_bb_filter", "range_bottom_bb"], default="breakout")
    p.add_argument("--breakout-buffer-pct", type=float, default=0.004)
    p.add_argument("--exit-buffer-pct", type=float, default=0.006)
    p.add_argument("--bb-window", type=int, default=20)
    p.add_argument("--bb-std", type=float, default=2.0)
    p.add_argument("--bb-near-lower-pct", type=float, default=0.025)
    p.add_argument("--bb-anti-chase-pct", type=float, default=0.035)
    p.add_argument("--initial-stop-pct", type=float, default=0.12)
    p.add_argument("--trail-stop-pct", type=float, default=0.18)
    p.add_argument("--exit-mode", choices=["level", "weekly_sma", "bb_mid", "trail_only", "rr"], default="level")
    p.add_argument("--weekly-exit-sma", type=int, default=20)
    p.add_argument("--target-r", type=float, default=2.0)
    p.add_argument("--max-position-weight", type=float, default=0.34)
    p.add_argument("--leverage", type=float, default=1.0)
    p.add_argument("--side-mode", choices=["long", "long_short"], default="long")
    p.add_argument("--fee-bps-per-side", type=float, default=5.0)
    p.add_argument("--slippage-bps-per-side", type=float, default=2.0)
    p.add_argument("--out-id", default=None)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    symbols = [s.strip() for s in str(args.symbols).split(",") if s.strip()]
    weekly_by_symbol = {symbol: _weekly_frame(symbol, args) for symbol in symbols}
    weekly_by_symbol = {k: v for k, v in weekly_by_symbol.items() if not v.empty}
    if not weekly_by_symbol:
        raise SystemExit("no symbol data")

    signals = {symbol: _signal_frame(frame, args) for symbol, frame in weekly_by_symbol.items()}
    result = _simulate(weekly_by_symbol, signals, args)
    buy_hold = _buy_hold_curve(weekly_by_symbol, args)
    summary = _summarize(result["equity"], result["trades"], buy_hold, args)
    out_dir = _write_outputs(result["equity"], result["trades"], buy_hold, summary, args)
    print(json.dumps({"out_dir": str(out_dir.relative_to(ROOT)), "summary": summary}, indent=2, sort_keys=True))
    return 0


def _weekly_frame(symbol: str, args: argparse.Namespace) -> pd.DataFrame:
    path = CACHE_DIR / f"{symbol}_futures_1d.parquet"
    if not path.exists():
        return pd.DataFrame()
    daily = pd.read_parquet(path).copy()
    daily.index = pd.to_datetime(daily.index, utc=True)
    daily = daily.sort_index()
    start = pd.Timestamp(args.start, tz="UTC")
    end = pd.Timestamp(args.end, tz="UTC")
    daily = daily.loc[(daily.index >= start) & (daily.index <= end)]
    for col in ("open", "high", "low", "close", "volume"):
        daily[col] = pd.to_numeric(daily[col], errors="coerce")
    daily = daily.dropna(subset=["open", "high", "low", "close"])
    if daily.empty:
        return pd.DataFrame()
    weekly = daily.resample("W-SUN").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
    monthly = daily.resample("ME").agg({"close": "last"}).dropna()
    monthly["monthly_sma"] = monthly["close"].rolling(int(args.monthly_sma), min_periods=max(3, int(args.monthly_sma) // 2)).mean()
    monthly["monthly_sma_slope"] = monthly["monthly_sma"] / monthly["monthly_sma"].shift(1) - 1.0
    weekly = weekly.join(monthly[["close", "monthly_sma", "monthly_sma_slope"]].rename(columns={"close": "monthly_close"}).reindex(weekly.index, method="ffill"))
    return weekly.dropna(subset=["monthly_close", "monthly_sma"])


def _signal_frame(weekly: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    out = weekly.copy()
    lookback = int(args.lookback_weeks)
    out["prior_high"] = out["high"].rolling(lookback, min_periods=max(8, lookback // 2)).max().shift(1)
    out["prior_low"] = out["low"].rolling(lookback, min_periods=max(8, lookback // 2)).min().shift(1)
    out["weekly_exit_sma"] = out["close"].rolling(int(args.weekly_exit_sma), min_periods=max(5, int(args.weekly_exit_sma) // 2)).mean()
    bb_window = int(args.bb_window)
    out["bb_mid"] = out["close"].rolling(bb_window, min_periods=max(8, bb_window // 2)).mean()
    bb_std = out["close"].rolling(bb_window, min_periods=max(8, bb_window // 2)).std()
    out["bb_upper"] = out["bb_mid"] + float(args.bb_std) * bb_std
    out["bb_lower"] = out["bb_mid"] - float(args.bb_std) * bb_std
    out["bb_width"] = (out["bb_upper"] - out["bb_lower"]) / out["bb_mid"]
    monthly_bull = (out["monthly_close"] > out["monthly_sma"]) & (out["monthly_sma_slope"] >= -0.01)
    monthly_bear = (out["monthly_close"] < out["monthly_sma"]) & (out["monthly_sma_slope"] <= 0.01)
    breakout_long = monthly_bull & (out["close"] > out["prior_high"] * (1.0 + float(args.breakout_buffer_pct)))
    if str(args.entry_mode) == "breakout_bb_filter":
        breakout_long &= out["close"] <= out["bb_upper"] * (1.0 + float(args.bb_anti_chase_pct))
    range_bottom_long = (
        (out["monthly_close"] >= out["monthly_sma"] * 0.90)
        & (out["low"] <= out["bb_lower"] * (1.0 + float(args.bb_near_lower_pct)))
        & (out["close"] >= out["bb_lower"])
        & ((out["close"] > out["open"]) | (out["close"] > out["close"].shift(1)))
        & (out["close"] <= out["bb_mid"] * 1.05)
    )
    if str(args.entry_mode) == "range_bottom_bb":
        out["long_signal"] = range_bottom_long
    else:
        out["long_signal"] = breakout_long
    out["short_signal"] = monthly_bear & (out["close"] < out["prior_low"] * (1.0 - float(args.breakout_buffer_pct)))
    if str(args.exit_mode) == "weekly_sma":
        out["long_thesis_ok"] = out["close"] >= out["weekly_exit_sma"] * (1.0 - float(args.exit_buffer_pct))
        out["short_thesis_ok"] = out["close"] <= out["weekly_exit_sma"] * (1.0 + float(args.exit_buffer_pct))
    elif str(args.exit_mode) == "bb_mid":
        out["long_thesis_ok"] = out["close"] >= out["bb_mid"] * (1.0 - float(args.exit_buffer_pct))
        out["short_thesis_ok"] = out["close"] <= out["bb_mid"] * (1.0 + float(args.exit_buffer_pct))
    elif str(args.exit_mode) == "trail_only":
        out["long_thesis_ok"] = True
        out["short_thesis_ok"] = True
    else:
        out["long_thesis_ok"] = monthly_bull & (out["close"] >= out["prior_high"] * (1.0 - float(args.exit_buffer_pct)))
        out["short_thesis_ok"] = monthly_bear & (out["close"] <= out["prior_low"] * (1.0 + float(args.exit_buffer_pct)))
    return out


def _simulate(
    weekly_by_symbol: dict[str, pd.DataFrame],
    signals: dict[str, pd.DataFrame],
    args: argparse.Namespace,
) -> dict[str, pd.DataFrame]:
    timeline = sorted(set().union(*[set(frame.index) for frame in weekly_by_symbol.values()]))
    active: dict[str, Position] = {}
    pending: list[tuple[str, str, float]] = []
    trades: list[dict[str, Any]] = []
    equity: list[dict[str, Any]] = []
    nav = float(args.initial_capital)
    fee_slip = (float(args.fee_bps_per_side) + float(args.slippage_bps_per_side)) / 10000.0
    max_weight = max(0.0, min(1.0, float(args.max_position_weight)))
    leverage = max(0.0, float(args.leverage))

    for idx, ts in enumerate(timeline):
        for symbol, side, level in pending:
            if symbol in active or symbol not in weekly_by_symbol or ts not in weekly_by_symbol[symbol].index:
                continue
            entry = float(weekly_by_symbol[symbol].loc[ts, "open"])
            if entry <= 0 or not math.isfinite(entry) or not math.isfinite(level):
                continue
            notional = nav * max_weight * leverage
            nav -= notional * fee_slip
            if side == "long":
                if str(args.exit_mode) == "rr":
                    stop = level * (1.0 - float(args.exit_buffer_pct))
                else:
                    stop = min(entry * (1.0 - float(args.initial_stop_pct)), level * (1.0 - float(args.exit_buffer_pct)))
                target = entry + max(0.0, entry - stop) * float(args.target_r)
                high_water = entry
                low_water = entry
            else:
                if str(args.exit_mode) == "rr":
                    stop = level * (1.0 + float(args.exit_buffer_pct))
                else:
                    stop = max(entry * (1.0 + float(args.initial_stop_pct)), level * (1.0 + float(args.exit_buffer_pct)))
                target = entry - max(0.0, stop - entry) * float(args.target_r)
                high_water = entry
                low_water = entry
            active[symbol] = Position(
                symbol=symbol,
                side=side,
                entry_ts=ts,
                entry_price=entry,
                notional=notional,
                level=level,
                stop=stop,
                target=target,
                high_water=high_water,
                low_water=low_water,
            )
        pending = []

        # Intrabar weekly stops for positions already open at the start of the week.
        for symbol in list(active):
            frame = weekly_by_symbol[symbol]
            if ts not in frame.index:
                continue
            row = frame.loc[ts]
            pos = active[symbol]
            if pos.side == "long":
                pos.high_water = max(pos.high_water, float(row["high"]))
                if str(args.exit_mode) != "rr":
                    pos.stop = max(pos.stop, pos.high_water * (1.0 - float(args.trail_stop_pct)))
                if float(row["low"]) <= pos.stop:
                    nav = _close_position(ts, symbol, pos, pos.stop, "stop_or_trail", nav, trades, fee_slip)
                    active.pop(symbol)
                    continue
                if str(args.exit_mode) == "rr" and float(row["high"]) >= pos.target:
                    nav = _close_position(ts, symbol, pos, pos.target, "target_r", nav, trades, fee_slip)
                    active.pop(symbol)
            else:
                pos.low_water = min(pos.low_water, float(row["low"]))
                if str(args.exit_mode) != "rr":
                    pos.stop = min(pos.stop, pos.low_water * (1.0 + float(args.trail_stop_pct)))
                if float(row["high"]) >= pos.stop:
                    nav = _close_position(ts, symbol, pos, pos.stop, "stop_or_trail", nav, trades, fee_slip)
                    active.pop(symbol)
                    continue
                if str(args.exit_mode) == "rr" and float(row["low"]) <= pos.target:
                    nav = _close_position(ts, symbol, pos, pos.target, "target_r", nav, trades, fee_slip)
                    active.pop(symbol)

        # Thesis exits are based on completed weekly close and execute at this close.
        for symbol in list(active):
            sig = signals[symbol]
            if ts not in sig.index:
                continue
            pos = active[symbol]
            row = sig.loc[ts]
            if str(args.exit_mode) == "rr":
                continue
            thesis_ok = bool(row["long_thesis_ok"]) if pos.side == "long" else bool(row["short_thesis_ok"])
            if not thesis_ok:
                nav = _close_position(ts, symbol, pos, float(row["close"]), "weekly_thesis_lost", nav, trades, fee_slip)
                active.pop(symbol)

        # Entries are based on completed weekly close and execute at this close.
        open_slots = max(0, int(math.floor(1.0 / max_weight)) - len(active)) if max_weight > 0 else 0
        if open_slots > 0:
            candidates = []
            for symbol, sig in signals.items():
                if symbol in active or ts not in sig.index:
                    continue
                row = sig.loc[ts]
                if bool(row["long_signal"]):
                    candidates.append((symbol, "long", float(row["prior_high"])))
                if str(args.side_mode) == "long_short" and bool(row["short_signal"]):
                    candidates.append((symbol, "short", float(row["prior_low"])))
            pending = candidates[:open_slots]

        mtm_nav = nav
        for symbol, pos in active.items():
            frame = weekly_by_symbol[symbol]
            if ts not in frame.index:
                continue
            px = float(frame.loc[ts, "close"])
            ret = px / pos.entry_price - 1.0
            if pos.side == "short":
                ret = -ret
            mtm_nav += pos.notional * (ret - fee_slip)
        equity.append({"ts": ts.isoformat(), "nav": mtm_nav, "realized_nav": nav, "open_positions": len(active)})

    final_ts = pd.Timestamp(timeline[-1])
    for symbol in list(active):
        pos = active[symbol]
        px = float(weekly_by_symbol[symbol].iloc[-1]["close"])
        nav = _close_position(final_ts, symbol, pos, px, "forced_end", nav, trades, fee_slip)
        active.pop(symbol)
    equity.append({"ts": final_ts.isoformat(), "nav": nav, "realized_nav": nav, "open_positions": 0})
    return {"equity": pd.DataFrame(equity), "trades": pd.DataFrame(trades)}


def _close_position(
    ts: pd.Timestamp,
    symbol: str,
    pos: Position,
    exit_price: float,
    reason: str,
    nav: float,
    trades: list[dict[str, Any]],
    fee_slip: float,
) -> float:
    gross = float(exit_price) / pos.entry_price - 1.0
    if pos.side == "short":
        gross = -gross
    net = gross - fee_slip
    notional = nav * 0.0  # placeholder for schema stability; pnl uses initial fixed risk below.
    trades.append(
        {
            "entry_ts": pos.entry_ts.isoformat(),
            "exit_ts": pd.Timestamp(ts).isoformat(),
            "symbol": symbol,
            "side": pos.side,
            "entry_price": pos.entry_price,
            "exit_price": float(exit_price),
            "notional": pos.notional,
            "level": pos.level,
            "stop": pos.stop,
            "target": pos.target,
            "gross_return": gross,
            "net_return": net,
            "exit_reason": reason,
            "hold_weeks": (pd.Timestamp(ts) - pos.entry_ts).days / 7.0,
        }
    )
    return nav + pos.notional * net


def _buy_hold_curve(weekly_by_symbol: dict[str, pd.DataFrame], args: argparse.Namespace) -> pd.DataFrame:
    closes = pd.concat({symbol: frame["close"] for symbol, frame in weekly_by_symbol.items()}, axis=1).dropna()
    returns = closes.pct_change().fillna(0.0)
    nav = float(args.initial_capital) * (1.0 + returns.mean(axis=1)).cumprod()
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
    bh = buy_hold.copy()
    bh_nav = pd.to_numeric(bh["nav"], errors="coerce")
    bh_ret = float(bh_nav.iloc[-1] / bh_nav.iloc[0] - 1.0)
    bh_ann = (1.0 + bh_ret) ** (365.0 / days) - 1.0 if bh_ret > -1 else -1.0
    if trades.empty:
        trade_metrics = {"trades": 0, "win_rate": math.nan, "avg_net_return": math.nan, "avg_hold_weeks": math.nan}
    else:
        ret = pd.to_numeric(trades["net_return"], errors="coerce")
        trade_metrics = {
            "trades": int(len(trades)),
            "win_rate": float((ret > 0).mean()),
            "avg_net_return": float(ret.mean()),
            "median_net_return": float(ret.median()),
            "avg_hold_weeks": float(pd.to_numeric(trades["hold_weeks"], errors="coerce").mean()),
            "by_symbol": {
                str(symbol): {
                    "trades": int(len(group)),
                    "win_rate": float((pd.to_numeric(group["net_return"], errors="coerce") > 0).mean()),
                    "avg_net_return": float(pd.to_numeric(group["net_return"], errors="coerce").mean()),
                }
                for symbol, group in trades.groupby("symbol")
            },
        }
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
        "buy_hold_annualized_return": float(bh_ann),
        **trade_metrics,
        "args": vars(args),
    }


def _write_outputs(equity: pd.DataFrame, trades: pd.DataFrame, buy_hold: pd.DataFrame, summary: dict[str, Any], args: argparse.Namespace) -> Path:
    out_id = args.out_id or f"weekly_key_level_breakout_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    out_dir = OUT_ROOT / out_id
    out_dir.mkdir(parents=True, exist_ok=True)
    equity.to_csv(out_dir / "equity_curve.csv", index=False)
    buy_hold.to_csv(out_dir / "buy_hold_curve.csv", index=False)
    trades.to_csv(out_dir / "trades.csv", index=False)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
    return out_dir


if __name__ == "__main__":
    raise SystemExit(main())
