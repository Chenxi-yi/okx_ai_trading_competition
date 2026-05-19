#!/usr/bin/env python3
"""Compare OKX stock-like USDT swap tokens with underlying US equity prices."""

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
CACHE_DIR = ROOT / "engine" / "data" / "cache"
OUT_ROOT = ROOT / "engine" / "data" / "research" / "okx_stock_token_tracking"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Research OKX stock token tracking vs real equities")
    p.add_argument("--symbols", default="AMD,AMZN,ARM,COIN,GOOGL,HOOD,INTC,MSTR,NVDA,TSLA")
    p.add_argument("--start", default="2025-01-01")
    p.add_argument("--end", default="2026-05-14")
    p.add_argument("--out-id", default=None)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    tickers = [item.strip().upper() for item in str(args.symbols).split(",") if item.strip()]
    rows: list[dict[str, Any]] = []
    for ticker in tickers:
        okx = _load_okx(ticker, args)
        equity = _load_yfinance(ticker, args)
        if okx.empty or equity.empty:
            rows.append({"ticker": ticker, "status": "missing_data", "okx_rows": len(okx), "equity_rows": len(equity), "return_corr": math.nan})
            continue
        rows.append(_compare(ticker, okx, equity))
    out_dir = OUT_ROOT / (args.out_id or f"stock_token_tracking_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}")
    out_dir.mkdir(parents=True, exist_ok=True)
    result = pd.DataFrame(rows).sort_values(["status", "return_corr"], ascending=[True, False])
    result.to_csv(out_dir / "tracking_summary.csv", index=False)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "args": vars(args),
        "summary": result.to_dict(orient="records"),
    }
    (out_dir / "summary.json").write_text(json.dumps(payload, indent=2, sort_keys=True))
    (out_dir / "report.md").write_text(_markdown(payload))
    print(json.dumps({"out_dir": str(out_dir.relative_to(ROOT)), "summary": payload["summary"]}, indent=2, sort_keys=True))
    return 0


def _load_okx(ticker: str, args: argparse.Namespace) -> pd.DataFrame:
    path = CACHE_DIR / f"{ticker}_USDT_futures_1d.parquet"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_parquet(path).copy()
    df.index = pd.to_datetime(df.index, utc=True).tz_convert(None).normalize()
    df = df.sort_index()
    start = pd.Timestamp(args.start)
    end = pd.Timestamp(args.end)
    df = df.loc[(df.index >= start) & (df.index <= end)]
    df["okx_close"] = pd.to_numeric(df["close"], errors="coerce")
    return df[["okx_close"]].dropna()


def _load_yfinance(ticker: str, args: argparse.Namespace) -> pd.DataFrame:
    try:
        import yfinance as yf
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("yfinance is required") from exc
    raw = yf.download(ticker, start=args.start, end=args.end, progress=False, auto_adjust=True)
    if raw.empty:
        return pd.DataFrame()
    close = raw["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    out = pd.DataFrame({"equity_close": pd.to_numeric(close, errors="coerce")})
    out.index = pd.to_datetime(out.index).tz_localize(None).normalize()
    return out.dropna()


def _compare(ticker: str, okx: pd.DataFrame, equity: pd.DataFrame) -> dict[str, Any]:
    joined = okx.join(equity, how="inner").dropna()
    if len(joined) < 20:
        return {"ticker": ticker, "status": "too_few_overlap", "overlap_days": int(len(joined))}
    joined["okx_ret"] = joined["okx_close"].pct_change()
    joined["equity_ret"] = joined["equity_close"].pct_change()
    ret = joined[["okx_ret", "equity_ret"]].dropna()
    diff = ret["okx_ret"] - ret["equity_ret"]
    level_ratio = joined["okx_close"] / joined["equity_close"]
    lead_corr = float(ret["okx_ret"].corr(ret["equity_ret"].shift(-1))) if len(ret) > 3 else math.nan
    lag_corr = float(ret["okx_ret"].corr(ret["equity_ret"].shift(1))) if len(ret) > 3 else math.nan
    return {
        "ticker": ticker,
        "status": "ok",
        "overlap_days": int(len(joined)),
        "start": joined.index.min().strftime("%Y-%m-%d"),
        "end": joined.index.max().strftime("%Y-%m-%d"),
        "return_corr": float(ret["okx_ret"].corr(ret["equity_ret"])),
        "okx_leads_equity_corr": lead_corr,
        "okx_lags_equity_corr": lag_corr,
        "tracking_error_daily": float(diff.std()),
        "mean_daily_ret_diff": float(diff.mean()),
        "abs_ret_diff_p95": float(diff.abs().quantile(0.95)),
        "large_dislocation_days_3pct": int((diff.abs() > 0.03).sum()),
        "level_ratio_mean": float(level_ratio.mean()),
        "level_ratio_std": float(level_ratio.std()),
        "okx_total_return": float(joined["okx_close"].iloc[-1] / joined["okx_close"].iloc[0] - 1.0),
        "equity_total_return": float(joined["equity_close"].iloc[-1] / joined["equity_close"].iloc[0] - 1.0),
    }


def _markdown(payload: dict[str, Any]) -> str:
    lines = ["# OKX Stock Token Tracking", "", f"Generated: {payload['generated_at']}", ""]
    lines.append("| Ticker | Days | Corr | Daily TE | p95 Abs Diff | 3% Disloc Days | OKX Ret | Equity Ret |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for row in payload["summary"]:
        if row.get("status") != "ok":
            lines.append(f"| {row.get('ticker')} | {row.get('overlap_days', 0)} | {row.get('status')} | | | | | |")
            continue
        lines.append(
            "| {ticker} | {overlap_days} | {return_corr:.3f} | {tracking_error_daily:.3%} | {abs_ret_diff_p95:.3%} | {large_dislocation_days_3pct} | {okx_total_return:.2%} | {equity_total_return:.2%} |".format(
                **row
            )
        )
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
