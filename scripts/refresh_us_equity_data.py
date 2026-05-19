#!/usr/bin/env python3
"""Refresh yfinance daily data for OKX stock-token research sleeves."""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "engine" / "data" / "cache" / "us_equities_yfinance_1d"
STATUS_PATH = ROOT / "engine" / "logs" / "data_refresh" / "us_equities_yfinance_status.json"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Refresh US equity daily cache via yfinance")
    p.add_argument("--symbols", default="AMD,AMZN,ARM,COIN,CRCL,GOOGL,HOOD,INTC,MSTR,NVDA,PLTR,TSLA")
    p.add_argument("--period", default="90d")
    p.add_argument("--interval", default="1d")
    p.add_argument("--loop", action="store_true")
    p.add_argument("--interval-sec", type=float, default=3600.0)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    while True:
        status = refresh_once(args)
        STATUS_PATH.write_text(json.dumps(status, indent=2, sort_keys=True))
        if not args.loop:
            return 0 if not status.get("failed") else 1
        time.sleep(max(60.0, float(args.interval_sec)))


def refresh_once(args: argparse.Namespace) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    symbols = [s.strip().upper() for s in str(args.symbols).split(",") if s.strip()]
    rows = []
    ok = 0
    failed = 0
    for ticker in symbols:
        try:
            frame = _download(ticker, args)
            if frame.empty:
                raise RuntimeError("empty yfinance frame")
            out_path = OUT_DIR / f"{ticker}.csv"
            frame.to_csv(out_path, index_label="date")
            ok += 1
            rows.append(
                {
                    "ticker": ticker,
                    "ok": True,
                    "rows": int(len(frame)),
                    "first_date": str(frame.index.min().date()),
                    "last_date": str(frame.index.max().date()),
                    "path": str(out_path.relative_to(ROOT)),
                }
            )
        except Exception as exc:
            failed += 1
            rows.append({"ticker": ticker, "ok": False, "error": str(exc)})
    completed = datetime.now(timezone.utc)
    return {
        "ok": failed == 0,
        "started_at": started.isoformat(),
        "completed_at": completed.isoformat(),
        "symbols": symbols,
        "success": ok,
        "failed": failed,
        "results": rows,
    }


def _download(ticker: str, args: argparse.Namespace) -> pd.DataFrame:
    try:
        import yfinance as yf
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("yfinance is required") from exc
    raw = yf.download(ticker, period=str(args.period), interval=str(args.interval), progress=False, auto_adjust=True)
    if raw.empty:
        return pd.DataFrame()
    close = raw["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    out = pd.DataFrame({"close": pd.to_numeric(close, errors="coerce")})
    out.index = pd.to_datetime(out.index).tz_localize(None).normalize()
    return out.dropna()


if __name__ == "__main__":
    raise SystemExit(main())
