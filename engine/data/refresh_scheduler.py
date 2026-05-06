#!/usr/bin/env python3
"""Unified market data refresh scheduler for paper and live runners.

The scheduler keeps the local data cache warm on a fixed cadence. Strategy
runners should consume these cache files instead of refreshing public data in
their own loops.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = ROOT / "engine"
sys.path.insert(0, str(ENGINE_DIR))

from config.settings import DATA_DIR  # noqa: E402
from data.fetcher import fetch_ohlcv  # noqa: E402


QUALITY_PATH = ENGINE_DIR / "data" / "quality" / "c_auto_dataset_quality_v1" / "symbol_quality.parquet"
LOG_DIR = ENGINE_DIR / "logs" / "data_refresh"
STATUS_PATH = LOG_DIR / "status.json"
PROGRESS_PATH = LOG_DIR / "progress.jsonl"
PID_PATH = ENGINE_DIR / "control" / "data_refresh.pid"

STOP = False


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run unified OKX market data refresh scheduler")
    p.add_argument("--interval-sec", type=float, default=900.0)
    p.add_argument("--max-symbols", type=int, default=30)
    p.add_argument("--timeframes", default="1h")
    p.add_argument("--lookback-days", type=int, default=3)
    p.add_argument("--sleep-sec", type=float, default=0.4)
    p.add_argument("--max-cycles", type=int, default=0)
    p.add_argument("--once", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    PID_PATH.write_text(str(__import__("os").getpid()))
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    cycles = 0
    while not STOP:
        started_at = datetime.now(timezone.utc)
        result = run_cycle(args, cycles + 1, started_at)
        cycles += 1
        _write_status(
            {
                **result,
                "scheduler_status": "stopped" if STOP else "running",
                "cycles": cycles,
                "interval_sec": args.interval_sec,
                "heartbeat_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        if args.once or (args.max_cycles > 0 and cycles >= args.max_cycles):
            break
        _sleep_interruptibly(max(5.0, float(args.interval_sec)))

    final = _read_json(STATUS_PATH) or {}
    final.update({"scheduler_status": "stopped", "heartbeat_at": datetime.now(timezone.utc).isoformat()})
    _write_status(final)
    return 0


def run_cycle(args: argparse.Namespace, cycle: int, started_at: datetime) -> dict[str, Any]:
    symbols = _symbols(int(args.max_symbols))
    timeframes = [item.strip() for item in str(args.timeframes).split(",") if item.strip()]
    target_end = pd.Timestamp.now(tz="UTC").floor("1h")
    start = (target_end - pd.Timedelta(days=int(args.lookback_days))).strftime("%Y-%m-%d")
    end = target_end.strftime("%Y-%m-%d")
    total_jobs = len(symbols) * len(timeframes)
    ok = 0
    failed = 0
    skipped = 0
    latest_records: list[dict[str, Any]] = []

    _write_status(
        {
            "scheduler_status": "running",
            "cycle": cycle,
            "cycle_started_at": started_at.isoformat(),
            "current_symbol": None,
            "current_timeframe": None,
            "target_end": target_end.isoformat(),
            "total_jobs": total_jobs,
            "ok": ok,
            "failed": failed,
            "skipped": skipped,
            "symbols": symbols,
            "timeframes": timeframes,
            "heartbeat_at": datetime.now(timezone.utc).isoformat(),
        }
    )

    for timeframe in timeframes:
        for symbol in symbols:
            if STOP:
                break
            record = _refresh_one(symbol, timeframe, start, end, target_end)
            latest_records.append(record)
            if record["status"] == "ok":
                ok += 1
            elif record["status"] == "skipped":
                skipped += 1
            else:
                failed += 1
            _append_jsonl(PROGRESS_PATH, record)
            _write_status(
                {
                    "scheduler_status": "running",
                    "cycle": cycle,
                    "cycle_started_at": started_at.isoformat(),
                    "current_symbol": symbol,
                    "current_timeframe": timeframe,
                    "target_end": target_end.isoformat(),
                    "total_jobs": total_jobs,
                    "ok": ok,
                    "failed": failed,
                    "skipped": skipped,
                    "last_record": record,
                    "symbols": symbols,
                    "timeframes": timeframes,
                    "heartbeat_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            _sleep_interruptibly(max(0.0, float(args.sleep_sec)))
    return {
        "cycle": cycle,
        "cycle_started_at": started_at.isoformat(),
        "cycle_finished_at": datetime.now(timezone.utc).isoformat(),
        "target_end": target_end.isoformat(),
        "total_jobs": total_jobs,
        "ok": ok,
        "failed": failed,
        "skipped": skipped,
        "last_records": latest_records[-20:],
        "symbols": symbols,
        "timeframes": timeframes,
    }


def _refresh_one(symbol: str, timeframe: str, start: str, end: str, target_end: pd.Timestamp) -> dict[str, Any]:
    started = time.time()
    try:
        before = _cache_max_ts(symbol, timeframe)
        df = fetch_ohlcv(
            symbol,
            start=start,
            end=end,
            mode="futures",
            timeframe=timeframe,
            use_cache=True,
            sandbox=False,
            fallback_to_stale=True,
            fallback_to_yfinance=False,
            include_funding=False,
        )
        after = _cache_max_ts(symbol, timeframe)
        return {
            "ts": datetime.now(timezone.utc).isoformat(),
            "status": "ok",
            "symbol": symbol,
            "timeframe": timeframe,
            "rows": int(len(df)) if df is not None else 0,
            "cache_before": before.isoformat() if before is not None else None,
            "cache_after": after.isoformat() if after is not None else None,
            "target_end": target_end.isoformat(),
            "elapsed_sec": round(time.time() - started, 3),
        }
    except Exception as exc:
        return {
            "ts": datetime.now(timezone.utc).isoformat(),
            "status": "failed",
            "symbol": symbol,
            "timeframe": timeframe,
            "error": str(exc),
            "target_end": target_end.isoformat(),
            "elapsed_sec": round(time.time() - started, 3),
        }


def _symbols(max_symbols: int) -> list[str]:
    if QUALITY_PATH.exists():
        df = pd.read_parquet(QUALITY_PATH)
        if "has_core_inputs" in df:
            df = df[df["has_core_inputs"].astype(bool)].copy()
        sort_cols = [col for col in ("train_eligible_180d", "1h_rows") if col in df.columns]
        if sort_cols:
            df = df.sort_values(sort_cols, ascending=[False] * len(sort_cols))
        symbols = []
        for symbol in df["symbol"].dropna().astype(str).tolist():
            if symbol not in symbols:
                symbols.append(symbol)
            if len(symbols) >= max_symbols:
                break
    else:
        symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
    if "BTC/USDT" not in symbols:
        symbols.insert(0, "BTC/USDT")
    return symbols[: max(max_symbols, 1)]


def _cache_max_ts(symbol: str, timeframe: str) -> pd.Timestamp | None:
    safe = symbol.replace("/", "_").replace(":", "_")
    for ext in ("parquet", "pkl"):
        path = DATA_DIR / f"{safe}_futures_{timeframe}.{ext}"
        if not path.exists():
            continue
        try:
            df = pd.read_parquet(path) if ext == "parquet" else pd.read_pickle(path)
            if df.empty:
                return None
            return pd.Timestamp(pd.to_datetime(df.index, utc=True).max())
        except Exception:
            return None
    return None


def _write_status(payload: dict[str, Any]) -> None:
    STATUS_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    with path.open("a") as fh:
        fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _sleep_interruptibly(seconds: float) -> None:
    deadline = time.time() + seconds
    while not STOP and time.time() < deadline:
        time.sleep(min(1.0, max(0.0, deadline - time.time())))


def _handle_stop(_signum: int, _frame: Any) -> None:
    global STOP
    STOP = True


if __name__ == "__main__":
    raise SystemExit(main())
