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
sys.path.insert(0, str(ROOT / "scripts"))

from config.settings import DATA_DIR  # noqa: E402
from data.fetcher import fetch_ohlcv  # noqa: E402
from data.frame_store import read_frame  # noqa: E402
from build_c_auto_feature_store import DEFAULT_DERIV_RUN  # noqa: E402
from fetch_derivatives_structure import (  # noqa: E402
    _create_okx as _create_derivatives_okx,
    _fetch_with_retries as _fetch_derivatives_with_retries,
    _resolve_symbol as _resolve_derivatives_symbol,
    _safe_symbol as _safe_derivatives_symbol,
)


QUALITY_PATH = ENGINE_DIR / "data" / "quality" / "c_auto_dataset_quality_rebuild_161_ohlcv_v1" / "symbol_quality.parquet"
LOG_DIR = ENGINE_DIR / "logs" / "data_refresh"
STATUS_PATH = LOG_DIR / "status.json"
PROGRESS_PATH = LOG_DIR / "progress.jsonl"
PID_PATH = ENGINE_DIR / "control" / "data_refresh.pid"

STOP = False


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run unified OKX market data refresh scheduler")
    p.add_argument("--interval-sec", type=float, default=900.0)
    p.add_argument("--max-symbols", type=int, default=150)
    p.add_argument("--extra-symbols", default="", help="Comma-separated symbols that registry strategies require in addition to the dynamic universe")
    p.add_argument("--timeframes", default="5m,15m,1h,4h,1d")
    p.add_argument("--lookback-days", type=int, default=3)
    p.add_argument("--sleep-sec", type=float, default=0.2)
    p.add_argument("--allow-stale-fallback", action="store_true")
    p.add_argument("--skip-derivatives", action="store_true")
    p.add_argument("--derivatives-run-id", default=DEFAULT_DERIV_RUN)
    p.add_argument("--derivatives-max-symbols", type=int, default=150)
    p.add_argument("--derivatives-kinds", default="funding,open_interest,long_short")
    p.add_argument("--derivatives-timeframe", default="5m")
    p.add_argument("--derivatives-lookback-days", type=int, default=3)
    p.add_argument("--derivatives-limit", type=int, default=100)
    p.add_argument("--derivatives-retry-attempts", type=int, default=3)
    p.add_argument("--derivatives-retry-sleep-sec", type=float, default=6.0)
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
    symbols = _symbols(int(args.max_symbols), extra_symbols=str(args.extra_symbols or ""))
    priority_symbols = [symbol for symbol in _parse_extra_symbols(str(args.extra_symbols or "")) if symbol in symbols]
    timeframes = _prioritize_timeframes([item.strip() for item in str(args.timeframes).split(",") if item.strip()])
    derivative_symbols = symbols[: max(0, min(int(args.derivatives_max_symbols), len(symbols)))]
    derivative_kinds = [item.strip() for item in str(args.derivatives_kinds).split(",") if item.strip()]
    target_end = pd.Timestamp.now(tz="UTC").floor("5min")
    start = (target_end - pd.Timedelta(days=int(args.lookback_days))).isoformat()
    end = target_end.isoformat()
    derivatives_start = (target_end - pd.Timedelta(days=int(args.derivatives_lookback_days))).strftime("%Y-%m-%d")
    derivatives_end = target_end.strftime("%Y-%m-%d")
    ohlcv_jobs = len(symbols) * len(timeframes)
    derivative_jobs = 0 if args.skip_derivatives else len(derivative_symbols) * len(derivative_kinds)
    total_jobs = ohlcv_jobs + derivative_jobs
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
            "derivatives": {
                "enabled": not bool(args.skip_derivatives),
                "run_id": args.derivatives_run_id,
                "symbols": derivative_symbols,
                "kinds": derivative_kinds,
                "timeframe": args.derivatives_timeframe,
            },
            "heartbeat_at": datetime.now(timezone.utc).isoformat(),
        }
    )

    critical_timeframes = [tf for tf in timeframes if tf in {"1h", "5m"}]
    deferred_timeframes = [tf for tf in timeframes if tf not in set(critical_timeframes)]
    for timeframe in critical_timeframes:
        ok, failed, skipped = _refresh_ohlcv_timeframe(
            args=args,
            cycle=cycle,
            started_at=started_at,
            symbols=symbols,
            timeframes=timeframes,
            derivative_symbols=derivative_symbols,
            derivative_kinds=derivative_kinds,
            timeframe=timeframe,
            start=start,
            end=end,
            target_end=target_end,
            total_jobs=total_jobs,
            ok=ok,
            failed=failed,
            skipped=skipped,
            latest_records=latest_records,
        )
    if priority_symbols:
        for timeframe in deferred_timeframes:
            ok, failed, skipped = _refresh_ohlcv_timeframe(
                args=args,
                cycle=cycle,
                started_at=started_at,
                symbols=priority_symbols,
                timeframes=timeframes,
                derivative_symbols=derivative_symbols,
                derivative_kinds=derivative_kinds,
                timeframe=timeframe,
                start=start,
                end=end,
                target_end=target_end,
                total_jobs=total_jobs,
                ok=ok,
                failed=failed,
                skipped=skipped,
                latest_records=latest_records,
            )
    if not args.skip_derivatives and not STOP:
        derivatives_ex = None
        for symbol in derivative_symbols:
            if STOP:
                break
            if derivatives_ex is None:
                derivatives_ex = _create_derivatives_okx()
            for kind in derivative_kinds:
                if STOP:
                    break
                record = _refresh_derivative_one(
                    ex=derivatives_ex,
                    symbol=symbol,
                    kind=kind,
                    timeframe=str(args.derivatives_timeframe),
                    start=derivatives_start,
                    end=derivatives_end,
                    target_end=_target_end_for_timeframe(str(args.derivatives_timeframe)),
                    run_id=str(args.derivatives_run_id),
                    limit=int(args.derivatives_limit),
                    attempts=int(args.derivatives_retry_attempts),
                    retry_sleep_sec=float(args.derivatives_retry_sleep_sec),
                )
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
                        "current_timeframe": str(args.derivatives_timeframe),
                        "current_kind": kind,
                        "target_end": target_end.isoformat(),
                        "total_jobs": total_jobs,
                        "ok": ok,
                        "failed": failed,
                        "skipped": skipped,
                        "last_record": record,
                        "symbols": symbols,
                        "timeframes": timeframes,
                        "derivatives": {
                            "enabled": True,
                            "run_id": args.derivatives_run_id,
                            "symbols": derivative_symbols,
                            "kinds": derivative_kinds,
                            "timeframe": args.derivatives_timeframe,
                        },
                        "heartbeat_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                _sleep_interruptibly(max(0.0, float(args.sleep_sec)))
    for timeframe in deferred_timeframes:
        remaining_symbols = [symbol for symbol in symbols if symbol not in set(priority_symbols)]
        if not remaining_symbols:
            continue
        ok, failed, skipped = _refresh_ohlcv_timeframe(
            args=args,
            cycle=cycle,
            started_at=started_at,
            symbols=remaining_symbols,
            timeframes=timeframes,
            derivative_symbols=derivative_symbols,
            derivative_kinds=derivative_kinds,
            timeframe=timeframe,
            start=start,
            end=end,
            target_end=target_end,
            total_jobs=total_jobs,
            ok=ok,
            failed=failed,
            skipped=skipped,
            latest_records=latest_records,
        )
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
        "derivatives": {
            "enabled": not bool(args.skip_derivatives),
            "run_id": args.derivatives_run_id,
            "symbols": derivative_symbols,
            "kinds": derivative_kinds,
            "timeframe": args.derivatives_timeframe,
        },
    }


def _prioritize_timeframes(timeframes: list[str]) -> list[str]:
    preferred = ["1h", "5m", "15m", "4h", "1d"]
    out: list[str] = []
    for timeframe in preferred + timeframes:
        if timeframe in timeframes and timeframe not in out:
            out.append(timeframe)
    return out


def _refresh_ohlcv_timeframe(
    *,
    args: argparse.Namespace,
    cycle: int,
    started_at: datetime,
    symbols: list[str],
    timeframes: list[str],
    derivative_symbols: list[str],
    derivative_kinds: list[str],
    timeframe: str,
    start: str,
    end: str,
    target_end: pd.Timestamp,
    total_jobs: int,
    ok: int,
    failed: int,
    skipped: int,
    latest_records: list[dict[str, Any]],
) -> tuple[int, int, int]:
    for symbol in symbols:
        if STOP:
            break
        record = _refresh_one(symbol, timeframe, start, end, _target_end_for_timeframe(timeframe), bool(args.allow_stale_fallback))
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
                "derivatives": {
                    "enabled": not bool(args.skip_derivatives),
                    "run_id": args.derivatives_run_id,
                    "symbols": derivative_symbols,
                    "kinds": derivative_kinds,
                    "timeframe": args.derivatives_timeframe,
                },
                "heartbeat_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        _sleep_interruptibly(max(0.0, float(args.sleep_sec)))
    return ok, failed, skipped


def _refresh_one(
    symbol: str,
    timeframe: str,
    start: str,
    end: str,
    target_end: pd.Timestamp,
    allow_stale_fallback: bool,
) -> dict[str, Any]:
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
            fallback_to_stale=allow_stale_fallback,
            fallback_to_yfinance=False,
            include_funding=False,
            cache_end_tolerance=_ohlcv_cache_end_tolerance(timeframe),
        )
        after = _cache_max_ts(symbol, timeframe)
        freshness = _freshness_status(after, target_end, timeframe)
        return {
            "ts": datetime.now(timezone.utc).isoformat(),
            "status": "ok" if freshness["fresh"] else "failed",
            "kind": "ohlcv",
            "symbol": symbol,
            "timeframe": timeframe,
            "rows": int(len(df)) if df is not None else 0,
            "cache_before": before.isoformat() if before is not None else None,
            "cache_after": after.isoformat() if after is not None else None,
            "target_end": target_end.isoformat(),
            "fresh": freshness["fresh"],
            "age_sec": freshness["age_sec"],
            "freshness_error": freshness.get("error"),
            "elapsed_sec": round(time.time() - started, 3),
        }
    except Exception as exc:
        after = _cache_max_ts(symbol, timeframe)
        freshness = _freshness_status(after, target_end, timeframe)
        return {
            "ts": datetime.now(timezone.utc).isoformat(),
            "status": "ok" if freshness["fresh"] else "failed",
            "kind": "ohlcv",
            "symbol": symbol,
            "timeframe": timeframe,
            "error": str(exc),
            "cache_after": after.isoformat() if after is not None else None,
            "fresh": freshness["fresh"],
            "age_sec": freshness["age_sec"],
            "freshness_error": freshness.get("error"),
            "target_end": target_end.isoformat(),
            "elapsed_sec": round(time.time() - started, 3),
        }


def _refresh_derivative_one(
    *,
    ex,
    symbol: str,
    kind: str,
    timeframe: str,
    start: str,
    end: str,
    target_end: pd.Timestamp,
    run_id: str,
    limit: int,
    attempts: int,
    retry_sleep_sec: float,
) -> dict[str, Any]:
    started = time.time()
    path = _derivative_path(run_id, symbol, kind, timeframe)
    try:
        before = _frame_max_ts(path)
        since = before + pd.Timedelta(milliseconds=1) if before is not None else pd.Timestamp(start, tz="UTC")
        min_since = pd.Timestamp(start, tz="UTC")
        since = max(since, min_since)
        end_ts = target_end
        ccxt_symbol = _resolve_derivatives_symbol(ex, symbol)
        df_new = _fetch_derivatives_with_retries(
            ex=ex,
            kind=kind,
            symbol=symbol,
            ccxt_symbol=ccxt_symbol,
            timeframe=timeframe,
            since_ms=int(since.timestamp() * 1000),
            end_ms=int(end_ts.timestamp() * 1000),
            limit=limit,
            attempts=attempts,
            retry_sleep_sec=retry_sleep_sec,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = _read_frame(path)
        merged = _merge_frames(existing, df_new)
        _write_frame(merged, path)
        after = _frame_max_ts(path)
        freshness_timeframe = "8h" if kind == "funding" else timeframe
        freshness = _freshness_status(after, target_end, freshness_timeframe)
        return {
            "ts": datetime.now(timezone.utc).isoformat(),
            "status": "ok" if freshness["fresh"] else "failed",
            "kind": kind,
            "symbol": symbol,
            "timeframe": timeframe,
            "rows": int(len(df_new)),
            "cache_before": before.isoformat() if before is not None else None,
            "cache_after": after.isoformat() if after is not None else None,
            "target_end": target_end.isoformat(),
            "artifact": str(path.relative_to(ENGINE_DIR)),
            "fresh": freshness["fresh"],
            "age_sec": freshness["age_sec"],
            "freshness_error": freshness.get("error"),
            "elapsed_sec": round(time.time() - started, 3),
        }
    except Exception as exc:
        return {
            "ts": datetime.now(timezone.utc).isoformat(),
            "status": "failed",
            "kind": kind,
            "symbol": symbol,
            "timeframe": timeframe,
            "error": str(exc),
            "target_end": target_end.isoformat(),
            "elapsed_sec": round(time.time() - started, 3),
        }


def _symbols(max_symbols: int, extra_symbols: str = "") -> list[str]:
    if QUALITY_PATH.exists():
        df = read_frame(QUALITY_PATH)
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
    for symbol in _parse_extra_symbols(extra_symbols):
        if symbol not in symbols:
            symbols.append(symbol)
    return symbols[: max(max_symbols, 1) + len(_parse_extra_symbols(extra_symbols))]


def _parse_extra_symbols(value: str) -> list[str]:
    out: list[str] = []
    for raw in str(value or "").split(","):
        symbol = raw.strip().upper().replace("-", "/")
        if not symbol:
            continue
        if "/" not in symbol and symbol.endswith("USDT"):
            symbol = f"{symbol[:-4]}/USDT"
        if symbol and symbol not in out:
            out.append(symbol)
    return out


def _target_end_for_timeframe(timeframe: str) -> pd.Timestamp:
    mapping = {"5m": "5min", "15m": "15min", "1h": "1h", "4h": "4h", "1d": "1d"}
    return pd.Timestamp.now(tz="UTC").floor(mapping.get(timeframe, "1h"))


def _freshness_status(cache_ts: pd.Timestamp | None, target_end: pd.Timestamp, timeframe: str) -> dict[str, Any]:
    if cache_ts is None:
        return {"fresh": False, "age_sec": None, "error": "missing_cache"}
    tolerance = _ohlcv_freshness_tolerance_seconds(timeframe)
    age_sec = max(0.0, (target_end - cache_ts).total_seconds())
    if age_sec > tolerance:
        return {"fresh": False, "age_sec": age_sec, "error": f"cache_lag_sec>{tolerance:g}"}
    return {"fresh": True, "age_sec": age_sec}


def _ohlcv_cache_end_tolerance(timeframe: str) -> pd.Timedelta:
    if timeframe == "5m":
        return pd.Timedelta(minutes=10)
    if timeframe == "15m":
        return pd.Timedelta(minutes=30)
    if timeframe == "1h":
        return pd.Timedelta(minutes=65)
    if timeframe == "4h":
        return pd.Timedelta(hours=4, minutes=15)
    if timeframe == "1d":
        return pd.Timedelta(days=1, hours=1)
    return pd.Timedelta(seconds=_timeframe_seconds(timeframe))


def _ohlcv_freshness_tolerance_seconds(timeframe: str) -> int:
    return int(_ohlcv_cache_end_tolerance(timeframe).total_seconds())


def _timeframe_seconds(timeframe: str) -> int:
    if timeframe == "5m":
        return 300
    if timeframe == "15m":
        return 900
    if timeframe == "1h":
        return 3600
    if timeframe == "4h":
        return 14400
    if timeframe == "8h":
        return 28800
    if timeframe == "1d":
        return 86400
    return 3600


def _cache_max_ts(symbol: str, timeframe: str) -> pd.Timestamp | None:
    safe = symbol.replace("/", "_").replace(":", "_")
    for ext in ("parquet", "pkl"):
        path = DATA_DIR / f"{safe}_futures_{timeframe}.{ext}"
        if not path.exists():
            continue
        try:
            df = read_frame(path, index_utc=True) if ext == "parquet" else pd.read_pickle(path)
            if df.empty:
                return None
            return pd.Timestamp(pd.to_datetime(df.index, utc=True).max())
        except Exception:
            return None
    return None


def _derivative_path(run_id: str, symbol: str, kind: str, timeframe: str) -> Path:
    return ENGINE_DIR / "data" / "derivatives_structure" / run_id / _safe_derivatives_symbol(symbol) / f"{kind}_{timeframe}.parquet"


def _frame_max_ts(path: Path) -> pd.Timestamp | None:
    df = _read_frame(path)
    if df.empty:
        return None
    try:
        return pd.Timestamp(pd.to_datetime(df.index, utc=True).max())
    except Exception:
        return None


def _read_frame(path: Path) -> pd.DataFrame:
    candidates = [path]
    if path.suffix == ".parquet":
        candidates.append(path.with_suffix(".pkl"))
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            df = read_frame(candidate, index_utc=True) if candidate.suffix == ".parquet" else pd.read_pickle(candidate)
            if not df.empty:
                df.index = pd.to_datetime(df.index, utc=True)
                return df.sort_index()
            return df
        except Exception:
            continue
    return pd.DataFrame()


def _merge_frames(existing: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    frames = [df for df in (existing, new) if df is not None and not df.empty]
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames).sort_index()
    if "symbol" in out.columns:
        out = out.reset_index().drop_duplicates(subset=["timestamp", "symbol"], keep="last").set_index("timestamp")
    else:
        out = out[~out.index.duplicated(keep="last")]
    out.index = pd.to_datetime(out.index, utc=True)
    return out.sort_index()


def _write_frame(df: pd.DataFrame, path: Path) -> None:
    try:
        df.to_parquet(path)
    except Exception:
        df.to_pickle(path.with_suffix(".pkl"))


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
