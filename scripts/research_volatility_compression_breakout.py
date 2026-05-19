#!/usr/bin/env python3
"""Research volatility-compression breakout signals on the C-Auto feature store."""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
FEATURE_DIR = ROOT / "engine" / "data" / "features"
OUT_ROOT = ROOT / "engine" / "data" / "research" / "volatility_compression_breakout"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Research compression breakout strategy")
    p.add_argument("--dataset-id", default="c_auto_feature_store_rebuild_161_ohlcv_snapshot_v1")
    p.add_argument("--start", default="2025-01-01")
    p.add_argument("--end", default="")
    p.add_argument("--max-symbols", type=int, default=80)
    p.add_argument("--min-volume-usd", type=float, default=200000)
    p.add_argument("--horizon-hours", type=int, default=12)
    p.add_argument("--compression-quantile", type=float, default=0.25)
    p.add_argument("--breakout-ret", type=float, default=0.006)
    p.add_argument("--oi-z-min", type=float, default=0.35)
    p.add_argument("--target-pct", type=float, default=0.035)
    p.add_argument("--stop-pct", type=float, default=0.018)
    p.add_argument("--fee-bps-per-side", type=float, default=5.0)
    p.add_argument("--slippage-bps-per-side", type=float, default=2.0)
    p.add_argument("--out-id", default="")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    df = _load(args)
    trades = _simulate(df, args)
    summary = _summarize(trades)
    out_dir = OUT_ROOT / (args.out_id or f"vol_breakout_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}")
    out_dir.mkdir(parents=True, exist_ok=True)
    trades.to_csv(out_dir / "trades.csv", index=False)
    payload = {"generated_at": datetime.now(timezone.utc).isoformat(), "args": vars(args), "summary": summary}
    (out_dir / "summary.json").write_text(json.dumps(payload, indent=2, sort_keys=True))
    (out_dir / "report.md").write_text(_markdown(payload, trades))
    print(json.dumps({"out_dir": str(out_dir.relative_to(ROOT)), "summary": summary}, indent=2, sort_keys=True))
    return 0


def _load(args: argparse.Namespace) -> pd.DataFrame:
    path = FEATURE_DIR / args.dataset_id / "features.parquet"
    cols = [
        "close",
        "volume_usd",
        "ret_1",
        "ret_3",
        "ret_6",
        "range_pct",
        "atr_14_pct",
        "close_to_high",
        "close_to_low",
        "oi_z_24",
        "funding_z_24",
        "btc_regime_6",
        "train_eligible_90d",
        "listing_age_days",
    ]
    df = pd.read_parquet(path, columns=cols).sort_index()
    start = pd.Timestamp(args.start, tz="UTC")
    end = pd.Timestamp(args.end, tz="UTC") if args.end else pd.Timestamp.now(tz="UTC")
    ts = df.index.get_level_values("timestamp")
    df = df[(ts >= start) & (ts <= end)].copy()
    for col in cols:
        if col != "btc_regime_6":
            df[col] = pd.to_numeric(df[col], errors="coerce")
    latest_vol = df.groupby(level="symbol")["volume_usd"].last().sort_values(ascending=False)
    keep = set(latest_vol.head(int(args.max_symbols)).index.astype(str))
    df = df[df.index.get_level_values("symbol").astype(str).isin(keep)]
    df = df[
        (df["volume_usd"].fillna(0) >= float(args.min_volume_usd))
        & (df["train_eligible_90d"].fillna(0) > 0)
        & (df["listing_age_days"].fillna(0) >= 30)
    ].copy()
    return df


def _simulate(df: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    horizon = max(1, int(args.horizon_hours))
    cost = 2.0 * (float(args.fee_bps_per_side) + float(args.slippage_bps_per_side)) / 10000.0
    grouped = df.groupby(level="symbol")
    fwd = grouped["close"].shift(-horizon) / df["close"] - 1.0
    compression = df["atr_14_pct"].groupby(level="symbol").transform(lambda s: s.rolling(120, min_periods=48).quantile(float(args.compression_quantile)))
    compressed = df["atr_14_pct"] <= compression
    long_signal = (
        compressed
        & (df["ret_1"] >= float(args.breakout_ret))
        & (df["close_to_high"] >= -0.004)
        & ((df["oi_z_24"].fillna(0) >= float(args.oi_z_min)) | (df["volume_usd"] > df["volume_usd"].groupby(level="symbol").transform(lambda s: s.rolling(48, min_periods=12).median()) * 1.5))
    )
    short_signal = (
        compressed
        & (df["ret_1"] <= -float(args.breakout_ret))
        & (df["close_to_low"] <= 0.004)
        & (df["oi_z_24"].fillna(0) >= float(args.oi_z_min))
    )
    rows = []
    last_exit: dict[str, pd.Timestamp] = {}
    events = df[(long_signal | short_signal) & fwd.notna()].copy()
    for (ts, symbol), row in events.iterrows():
        ts = pd.Timestamp(ts)
        symbol = str(symbol)
        if symbol in last_exit and ts <= last_exit[symbol]:
            continue
        side = "long" if bool(long_signal.loc[(ts, symbol)]) else "short"
        gross = float(fwd.loc[(ts, symbol)])
        if side == "short":
            gross = -gross
        if gross >= float(args.target_pct):
            realized = float(args.target_pct)
            reason = "target_proxy"
        elif gross <= -float(args.stop_pct):
            realized = -float(args.stop_pct)
            reason = "stop_proxy"
        else:
            realized = gross
            reason = "horizon_proxy"
        rows.append(
            {
                "entry_ts": ts.isoformat(),
                "exit_ts": (ts + pd.Timedelta(hours=horizon)).isoformat(),
                "symbol": symbol,
                "side": side,
                "btc_regime_6": str(row.get("btc_regime_6") or ""),
                "gross_return": realized,
                "net_return": realized - cost,
                "exit_reason": reason,
                "atr_14_pct": float(row["atr_14_pct"]),
                "ret_1": float(row["ret_1"]),
                "oi_z_24": float(row.get("oi_z_24") or 0.0),
            }
        )
        last_exit[symbol] = ts + pd.Timedelta(hours=horizon)
    return pd.DataFrame(rows)


def _summarize(trades: pd.DataFrame) -> dict[str, Any]:
    if trades.empty:
        return {"trades": 0, "win_rate": None, "sum_net_return": 0.0}
    ret = pd.to_numeric(trades["net_return"], errors="coerce").dropna()
    monthly = trades.assign(month=pd.to_datetime(trades["entry_ts"], utc=True).dt.strftime("%Y-%m")).groupby("month")["net_return"].sum()
    return {
        "trades": int(len(ret)),
        "win_rate": float((ret > 0).mean()),
        "mean_net_return": float(ret.mean()),
        "sum_net_return": float(ret.sum()),
        "positive_month_rate": float((monthly > 0).mean()) if len(monthly) else None,
        "worst_month": float(monthly.min()) if len(monthly) else None,
        "sharpe_like": float(ret.mean() / ret.std() * math.sqrt(365 * 24 / max(1, 12))) if len(ret) > 2 and float(ret.std()) > 0 else math.nan,
    }


def _markdown(payload: dict[str, Any], trades: pd.DataFrame) -> str:
    s = payload["summary"]
    lines = ["# Volatility Compression Breakout", "", f"Generated: {payload['generated_at']}", ""]
    lines.append(
        "Trades={trades}, win={win}, sum={sum:.2%}, avg={avg:.2%}, positive_month={pm}".format(
            trades=s.get("trades"),
            win="n/a" if s.get("win_rate") is None else f"{s['win_rate']:.1%}",
            sum=float(s.get("sum_net_return") or 0.0),
            avg=float(s.get("mean_net_return") or 0.0),
            pm="n/a" if s.get("positive_month_rate") is None else f"{s['positive_month_rate']:.1%}",
        )
    )
    if not trades.empty:
        lines.extend(["", "## By Side", "", "| Side | Trades | Win | Sum Net |", "|---|---:|---:|---:|"])
        for side, sample in trades.groupby("side"):
            ret = sample["net_return"].astype(float)
            lines.append(f"| {side} | {len(ret)} | {(ret > 0).mean():.1%} | {ret.sum():.2%} |")
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
