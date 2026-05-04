#!/usr/bin/env python3
"""Download broad OKX futures history for model training."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
ENGINE_DIR = ROOT / "engine"
sys.path.insert(0, str(ENGINE_DIR))

from data.fetcher import fetch_ohlcv, fetch_tradable_futures_symbols

OUT_ROOT = ENGINE_DIR / "data" / "training_history"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Fetch broad OKX USDT-swap OHLCV/funding history")
    p.add_argument("--run-id", default=None)
    p.add_argument("--symbols", default=None, help="Comma-separated ccxt symbols. Overrides universe discovery.")
    p.add_argument("--symbols-manifest", default=None, help="Load symbols from an existing manifest JSON.")
    p.add_argument("--min-volume-usd", type=float, default=30_000_000.0)
    p.add_argument("--max-symbols", type=int, default=250)
    p.add_argument("--start", default="2024-01-01")
    p.add_argument("--end", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    p.add_argument("--timeframes", default="1h", help="Comma-separated ccxt timeframes, e.g. 1h,5m")
    p.add_argument("--sleep-sec", type=float, default=0.5)
    p.add_argument("--retry-attempts", type=int, default=4)
    p.add_argument("--retry-sleep-sec", type=float, default=8.0)
    p.add_argument("--min-coverage", type=float, default=0.8, help="Minimum expected bar coverage before a symbol is marked ok")
    p.add_argument("--min-rows", type=int, default=100, help="Minimum rows required before a symbol is marked ok")
    p.add_argument("--skip-funding", action="store_true", help="Skip funding-rate join while downloading OHLCV")
    p.add_argument("--refresh-universe", action="store_true", help="Refresh universe even if manifest exists")
    p.add_argument("--discover-only", action="store_true", help="Only discover/write universe manifest; do not download symbol history")
    p.add_argument("--log-level", default="INFO")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run_id = args.run_id or f"train_hist_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    out_dir = OUT_ROOT / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    progress_path = out_dir / "progress.jsonl"
    manifest_path = out_dir / "manifest.json"
    status_path = out_dir / "status.json"
    timeframes = [t.strip() for t in args.timeframes.split(",") if t.strip()]

    manifest = _load_manifest(manifest_path)
    fixed_symbols = _load_fixed_symbols(args.symbols, args.symbols_manifest)
    if fixed_symbols and (args.refresh_universe or not manifest.get("symbols")):
        symbols = fixed_symbols
        manifest.update(
            {
                "run_id": run_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source_manifest": _relpath(Path(args.symbols_manifest).resolve()) if args.symbols_manifest else None,
                "symbols": symbols,
                "start": args.start,
                "end": args.end,
                "timeframes": timeframes,
                "skip_funding": bool(args.skip_funding),
                "status": "running",
                "summary": {
                    "ok": 0,
                    "failed": 0,
                    "skipped_existing": 0,
                    "total_jobs": len(symbols) * len(timeframes),
                },
            }
        )
        _write_json(manifest_path, manifest)
    elif args.refresh_universe or not manifest.get("symbols"):
        symbols = fetch_tradable_futures_symbols(
            min_daily_volume_usd=args.min_volume_usd,
            max_symbols=args.max_symbols,
            sandbox=False,
        )
        manifest.update(
            {
                "run_id": run_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "min_volume_usd": args.min_volume_usd,
                "max_symbols": args.max_symbols,
                "symbols": symbols,
                "start": args.start,
                "end": args.end,
                "timeframes": timeframes,
                "skip_funding": bool(args.skip_funding),
                "status": "running",
            }
        )
        _write_json(manifest_path, manifest)
    else:
        symbols = list(manifest["symbols"])

    completed = _completed(progress_path)
    total_jobs = len(symbols) * len(timeframes)
    logging.info("Training history run=%s symbols=%d timeframes=%s total_jobs=%d", run_id, len(symbols), timeframes, total_jobs)
    if args.discover_only:
        manifest.update(
            {
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "status": "discovered",
                "summary": {
                    "ok": 0,
                    "failed": 0,
                    "skipped_existing": 0,
                    "total_jobs": total_jobs,
                },
            }
        )
        _write_json(manifest_path, manifest)
        print(json.dumps(manifest, indent=2, sort_keys=True))
        return 0

    ok = 0
    failed = 0
    skipped = 0
    for timeframe in timeframes:
        for i, symbol in enumerate(symbols, start=1):
            key = (symbol, timeframe)
            if key in completed:
                skipped += 1
                continue
            started = time.time()
            _write_json(
                status_path,
                {
                    "run_id": run_id,
                    "status": "running",
                    "current_symbol": symbol,
                    "current_timeframe": timeframe,
                    "job_index": i,
                    "symbols": len(symbols),
                    "total_jobs": total_jobs,
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            record: Dict = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "symbol": symbol,
                "timeframe": timeframe,
                "start": args.start,
                "end": args.end,
            }
            try:
                expected_rows = _expected_rows(args.start, args.end, timeframe)
                min_rows = max(int(args.min_rows), 1)
                df = _fetch_with_retries(
                    symbol,
                    args.start,
                    args.end,
                    timeframe,
                    args.retry_attempts,
                    args.retry_sleep_sec,
                    min_rows=min_rows,
                    include_funding=not args.skip_funding,
                )
                coverage = len(df) / max(expected_rows, 1)
                record.update(
                    {
                        "status": "ok",
                        "rows": int(len(df)),
                        "expected_rows": int(expected_rows),
                        "coverage": round(float(coverage), 6),
                        "min_rows": int(min_rows),
                        "first_ts": str(df.index.min()) if not df.empty else None,
                        "last_ts": str(df.index.max()) if not df.empty else None,
                        "elapsed_sec": round(time.time() - started, 3),
                    }
                )
                ok += 1
                logging.info("OK %s %s rows=%d (%d/%d)", symbol, timeframe, len(df), i, len(symbols))
            except Exception as exc:
                record.update({"status": "failed", "error": str(exc), "elapsed_sec": round(time.time() - started, 3)})
                failed += 1
                logging.warning("FAILED %s %s: %s", symbol, timeframe, exc)
            _append_jsonl(progress_path, record)
            _write_json(
                status_path,
                {
                    "run_id": run_id,
                    "status": "running",
                    "last_symbol": symbol,
                    "last_timeframe": timeframe,
                    "last_record": record,
                    "job_index": i,
                    "symbols": len(symbols),
                    "total_jobs": total_jobs,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            time.sleep(max(0.0, args.sleep_sec))

    manifest.update(
        {
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "status": "completed",
            "summary": {
                "ok": ok,
                "failed": failed,
                "skipped_existing": skipped,
                "total_jobs": total_jobs,
            },
        }
    )
    _write_json(manifest_path, manifest)
    _write_json(
        status_path,
        {
            "run_id": run_id,
            "status": "completed",
            "summary": manifest["summary"],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0 if failed == 0 else 1


def _fetch_with_retries(
    symbol: str,
    start: str,
    end: str,
    timeframe: str,
    attempts: int,
    sleep_sec: float,
    min_rows: int,
    include_funding: bool = True,
):
    last_exc: Exception | None = None
    for attempt in range(max(1, attempts)):
        try:
            df = fetch_ohlcv(
                symbol,
                start=start,
                end=end,
                mode="futures",
                timeframe=timeframe,
                use_cache=True,
                sandbox=False,
                fallback_to_stale=False,
                fallback_to_yfinance=False,
                include_funding=include_funding,
            )
            if len(df) < min_rows:
                raise RuntimeError(f"insufficient history coverage rows={len(df)} min_rows={min_rows}")
            return df
        except Exception as exc:
            last_exc = exc
            if _is_fast_fail_error(exc):
                break
            if attempt < attempts - 1:
                time.sleep(sleep_sec * (attempt + 1))
    raise last_exc or RuntimeError("fetch failed")


def _is_fast_fail_error(exc: Exception) -> bool:
    msg = str(exc)
    fast_fail_markers = (
        "No OHLCV data returned",
        "Symbol not found",
        "does not have market symbol",
        "market not found",
    )
    return any(marker in msg for marker in fast_fail_markers)


def _expected_rows(start: str, end: str, timeframe: str) -> int:
    start_ts = pd.Timestamp(start, tz="UTC")
    end_ts = pd.Timestamp(end, tz="UTC")
    if len(end) == 10 and timeframe != "1d":
        end_ts = end_ts + pd.Timedelta(days=1)
    delta = end_ts - start_ts
    freq = _pandas_timeframe(timeframe)
    return max(int(delta / pd.Timedelta(freq)), 1)


def _pandas_timeframe(timeframe: str) -> str:
    if timeframe.endswith("m") and timeframe[:-1].isdigit():
        return f"{timeframe[:-1]}min"
    return timeframe


def _load_manifest(path: Path) -> Dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _load_fixed_symbols(raw_symbols: str | None, manifest_path: str | None) -> List[str]:
    if raw_symbols:
        return [s.strip() for s in raw_symbols.split(",") if s.strip()]
    if manifest_path:
        payload = json.loads(Path(manifest_path).resolve().read_text())
        return [s.strip() for s in payload.get("symbols", []) if s and s.strip()]
    return []


def _relpath(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def _completed(path: Path) -> set[tuple[str, str]]:
    done = set()
    if not path.exists():
        return done
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("status") == "ok":
            done.add((record.get("symbol"), record.get("timeframe")))
    return done


def _append_jsonl(path: Path, record: Dict) -> None:
    with path.open("a") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def _write_json(path: Path, payload: Dict) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
