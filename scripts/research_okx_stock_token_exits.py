#!/usr/bin/env python3
"""Research stock-token momentum exits with intraday proxy stop/target rules."""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT / "engine" / "data" / "cache"
OUT_ROOT = ROOT / "engine" / "data" / "research" / "okx_stock_token_exits"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backtest stock token exit overlays")
    p.add_argument("--symbols", default="AMZN,COIN,GOOGL,HOOD,NVDA,TSLA")
    p.add_argument("--start", default="2026-02-01")
    p.add_argument("--end", default="2026-05-16")
    p.add_argument("--threshold", type=float, default=0.02)
    p.add_argument("--fee-bps-per-side", type=float, default=5.0)
    p.add_argument("--slippage-bps-per-side", type=float, default=2.0)
    p.add_argument("--out-id", default="")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    rows: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    for ticker in [s.strip().upper() for s in str(args.symbols).split(",") if s.strip()]:
        okx = _load_okx(ticker, args)
        equity = _load_equity_cache(ticker, args)
        joined = okx.join(equity, how="inner").dropna()
        if len(joined) < 20:
            rows.append({"ticker": ticker, "strategy": "all", "status": "too_few_overlap", "days": len(joined)})
            continue
        joined["okx_ret"] = joined["okx_close"].pct_change()
        joined["equity_ret"] = joined["equity_close"].pct_change()
        joined["dislocation"] = joined["okx_ret"] - joined["equity_ret"]
        signals = _signals(joined, args)
        for strategy, signal in signals.items():
            for exit_name, exit_cfg in _exit_configs().items():
                sample = _simulate(ticker, strategy, signal, joined, exit_name, exit_cfg, args)
                rows.append(_summary(ticker, strategy, exit_name, sample))
                trades.extend(sample)
    out_dir = OUT_ROOT / (args.out_id or f"stock_token_exits_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}")
    out_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(out_dir / "summary.csv", index=False)
    pd.DataFrame(trades).to_csv(out_dir / "trades.csv", index=False)
    payload = {"generated_at": datetime.now(timezone.utc).isoformat(), "args": vars(args), "summary": rows}
    (out_dir / "summary.json").write_text(json.dumps(payload, indent=2, sort_keys=True))
    (out_dir / "report.md").write_text(_markdown(payload))
    print(json.dumps({"out_dir": str(out_dir.relative_to(ROOT)), "summary": rows}, indent=2, sort_keys=True))
    return 0


def _load_okx(ticker: str, args: argparse.Namespace) -> pd.DataFrame:
    path = CACHE_DIR / f"{ticker}_USDT_futures_1d.parquet"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_parquet(path).copy()
    df.index = pd.to_datetime(df.index, utc=True).tz_convert(None).normalize()
    df = df.sort_index()
    df = df.loc[(df.index >= pd.Timestamp(args.start)) & (df.index <= pd.Timestamp(args.end))]
    out = pd.DataFrame(index=df.index)
    for col in ("open", "high", "low", "close"):
        out[f"okx_{col}"] = pd.to_numeric(df[col], errors="coerce")
    return out.dropna()


def _load_equity_cache(ticker: str, args: argparse.Namespace) -> pd.DataFrame:
    path = CACHE_DIR / "us_equities_yfinance_1d" / f"{ticker}.csv"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path)
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    df = df.set_index("date").sort_index()
    df = df.loc[(df.index >= pd.Timestamp(args.start)) & (df.index <= pd.Timestamp(args.end))]
    return pd.DataFrame({"equity_close": pd.to_numeric(df["close"], errors="coerce")}).dropna()


def _signals(df: pd.DataFrame, args: argparse.Namespace) -> dict[str, pd.Series]:
    th = float(args.threshold)
    okx_confirm = pd.Series(0.0, index=df.index)
    okx_confirm.loc[(df["okx_ret"] > th) & (df["equity_ret"] >= 0.01)] = 1.0
    okx_confirm.loc[(df["okx_ret"] < -th) & (df["equity_ret"] <= -0.01)] = -1.0
    equity_momo = pd.Series(0.0, index=df.index)
    equity_momo.loc[df["equity_ret"] > th] = 1.0
    equity_momo.loc[df["equity_ret"] < -th] = -1.0
    return {
        "okx_momentum_equity_confirm": okx_confirm,
        "equity_momentum": equity_momo,
    }


def _exit_configs() -> dict[str, dict[str, float]]:
    return {
        "base_4_6": {"stop": 0.04, "target": 0.06, "breakeven": 0.0, "trail": 0.0},
        "breakeven_after_2": {"stop": 0.04, "target": 0.06, "breakeven": 0.02, "trail": 0.0},
        "trail_after_3": {"stop": 0.04, "target": 0.08, "breakeven": 0.02, "trail": 0.03},
        "quick_take_3": {"stop": 0.035, "target": 0.03, "breakeven": 0.0, "trail": 0.0},
    }


def _simulate(
    ticker: str,
    strategy: str,
    signal: pd.Series,
    df: pd.DataFrame,
    exit_name: str,
    cfg: dict[str, float],
    args: argparse.Namespace,
) -> list[dict[str, Any]]:
    cost = 2.0 * (float(args.fee_bps_per_side) + float(args.slippage_bps_per_side)) / 10000.0
    rows = []
    for ts, side_value in signal.items():
        if side_value == 0 or ts not in df.index:
            continue
        entry_pos = df.index.get_loc(ts) + 1
        if entry_pos >= len(df):
            continue
        nxt = df.iloc[entry_pos]
        entry_ts = df.index[entry_pos]
        entry = float(nxt["okx_open"])
        if entry <= 0:
            continue
        high_ret = float(nxt["okx_high"]) / entry - 1.0
        low_ret = float(nxt["okx_low"]) / entry - 1.0
        close_ret = float(nxt["okx_close"]) / entry - 1.0
        if side_value < 0:
            fav = -low_ret
            adv = high_ret
            close_gross = -close_ret
        else:
            fav = high_ret
            adv = -low_ret
            close_gross = close_ret
        gross = close_gross
        reason = "close"
        if adv >= float(cfg["stop"]):
            gross = -float(cfg["stop"])
            reason = "stop"
        if fav >= float(cfg["target"]):
            gross = float(cfg["target"])
            reason = "target"
        if reason == "close" and float(cfg.get("breakeven") or 0.0) > 0 and fav >= float(cfg["breakeven"]) and close_gross < 0:
            gross = 0.0
            reason = "breakeven"
        if reason == "close" and float(cfg.get("trail") or 0.0) > 0 and fav >= float(cfg["trail"]):
            gross = max(close_gross, max(0.0, fav - float(cfg["trail"])))
            reason = "trail"
        rows.append(
            {
                "ticker": ticker,
                "strategy": strategy,
                "exit_rule": exit_name,
                "signal_ts": ts.strftime("%Y-%m-%d"),
                "entry_ts": entry_ts.strftime("%Y-%m-%d"),
                "side": "long" if side_value > 0 else "short",
                "gross_return": gross,
                "net_return": gross - cost,
                "exit_reason": reason,
            }
        )
    return rows


def _summary(ticker: str, strategy: str, exit_name: str, trades: list[dict[str, Any]]) -> dict[str, Any]:
    if not trades:
        return {"ticker": ticker, "strategy": strategy, "exit_rule": exit_name, "status": "empty", "trades": 0}
    ret = pd.Series([float(t["net_return"]) for t in trades])
    return {
        "ticker": ticker,
        "strategy": strategy,
        "exit_rule": exit_name,
        "status": "ok",
        "trades": int(len(ret)),
        "win_rate": float((ret > 0).mean()),
        "avg_net_return": float(ret.mean()),
        "sum_net_return": float(ret.sum()),
        "sharpe_like": float(ret.mean() / ret.std() * math.sqrt(252)) if len(ret) > 2 and float(ret.std()) > 0 else math.nan,
    }


def _markdown(payload: dict[str, Any]) -> str:
    rows = [r for r in payload["summary"] if r.get("status") == "ok"]
    lines = ["# OKX Stock Token Exit Overlay Research", "", f"Generated: {payload['generated_at']}", ""]
    lines.append("| Strategy | Exit | Trades | Win | Avg Net | Sum Net | Sharpe-like |")
    lines.append("|---|---|---:|---:|---:|---:|---:|")
    df = pd.DataFrame(rows)
    if not df.empty:
        grouped = df.groupby(["strategy", "exit_rule"]).agg(
            trades=("trades", "sum"),
            sum_net_return=("sum_net_return", "sum"),
            avg_net_return=("avg_net_return", "mean"),
            win_rate=("win_rate", "mean"),
            sharpe_like=("sharpe_like", "mean"),
        )
        for (strategy, exit_rule), row in grouped.sort_values("sum_net_return", ascending=False).iterrows():
            lines.append(
                f"| {strategy} | {exit_rule} | {int(row['trades'])} | {row['win_rate']:.1%} | {row['avg_net_return']:.2%} | {row['sum_net_return']:.2%} | {row['sharpe_like']:.2f} |"
            )
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
