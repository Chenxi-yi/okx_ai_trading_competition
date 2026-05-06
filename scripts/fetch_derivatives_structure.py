#!/usr/bin/env python3
"""Download OKX derivatives structure history for a broad swap universe."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

import ccxt
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
ENGINE_DIR = ROOT / "engine"
sys.path.insert(0, str(ENGINE_DIR))

OUT_ROOT = ENGINE_DIR / "data" / "derivatives_structure"
DEFAULT_SOURCE_MANIFEST = ENGINE_DIR / "data" / "training_history" / "train_hist_vol1m_1h_20240101_20260424" / "manifest.json"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Fetch OKX derivatives structure data")
    p.add_argument("--run-id", default=None)
    p.add_argument("--symbols", default=None, help="Comma-separated ccxt symbols. Defaults to --symbols-manifest symbols.")
    p.add_argument("--symbols-manifest", default=str(DEFAULT_SOURCE_MANIFEST))
    p.add_argument("--start", default=None)
    p.add_argument("--end", default=None)
    p.add_argument("--timeframe", default="5m")
    p.add_argument("--kinds", default="funding,open_interest,long_short")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--sleep-sec", type=float, default=1.0)
    p.add_argument("--retry-attempts", type=int, default=4)
    p.add_argument("--retry-sleep-sec", type=float, default=8.0)
    p.add_argument("--refresh-manifest", action="store_true")
    p.add_argument("--discover-only", action="store_true", help="Only write the derivatives manifest; do not download data")
    p.add_argument("--log-level", default="INFO")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run_id = args.run_id or f"deriv_struct_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    out_dir = OUT_ROOT / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "manifest.json"
    progress_path = out_dir / "progress.jsonl"
    status_path = out_dir / "status.json"

    source_manifest_path = Path(args.symbols_manifest).resolve() if args.symbols_manifest else None
    source_manifest = _load_json(source_manifest_path) if source_manifest_path else {}
    symbols = _parse_symbols(args.symbols) if args.symbols else list(source_manifest.get("symbols", []))
    if not symbols:
        raise SystemExit("no symbols supplied")
    kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
    start = args.start or source_manifest.get("start") or "2024-01-01"
    end = args.end or source_manifest.get("end") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    timeframe = args.timeframe
    limit = args.limit

    manifest = _load_json(manifest_path)
    if manifest and not args.refresh_manifest:
        symbols = list(manifest.get("symbols") or symbols)
        kinds = list(manifest.get("kinds") or kinds)
        start = str(manifest.get("start") or start)
        end = str(manifest.get("end") or end)
        timeframe = str(manifest.get("timeframe") or timeframe)
        limit = int(manifest.get("limit") or limit)
    total_jobs = len(symbols) * len(kinds)
    if args.refresh_manifest or not manifest:
        manifest = {
            "download_type": "derivatives_structure",
            "run_id": run_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_manifest": _relpath(source_manifest_path) if source_manifest_path else None,
            "symbols": symbols,
            "kinds": kinds,
            "start": start,
            "end": end,
            "timeframe": timeframe,
            "limit": limit,
            "status": "running",
            "summary": {"ok": 0, "failed": 0, "skipped_existing": 0, "total_jobs": total_jobs},
        }
        _write_json(manifest_path, manifest)

    completed = _completed(progress_path)
    ok = 0
    failed = 0
    skipped = 0
    ex = _create_okx()
    since_ms = _to_ms(start)
    end_ms = _to_ms(end, end_of_day=True)

    logging.info("Derivatives structure run=%s symbols=%d kinds=%s total_jobs=%d", run_id, len(symbols), kinds, total_jobs)
    if args.discover_only:
        manifest.update(
            {
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "status": "discovered",
                "summary": {"ok": 0, "failed": 0, "skipped_existing": 0, "total_jobs": total_jobs},
            }
        )
        _write_json(manifest_path, manifest)
        print(json.dumps(manifest, indent=2, sort_keys=True))
        return 0

    job_index = 0
    for symbol in symbols:
        ccxt_symbol = _resolve_symbol(ex, symbol)
        for kind in kinds:
            job_index += 1
            key = (symbol, kind, timeframe)
            if key in completed:
                skipped += 1
                continue
            _write_json(
                status_path,
                {
                    "download_type": "derivatives_structure",
                    "run_id": run_id,
                    "status": "running",
                    "current_symbol": symbol,
                    "current_kind": kind,
                    "current_timeframe": timeframe,
                    "job_index": job_index,
                    "total_jobs": total_jobs,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            started = time.time()
            record: Dict[str, Any] = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "symbol": symbol,
                "kind": kind,
                "timeframe": timeframe,
                "start": start,
                "end": end,
            }
            try:
                df = _fetch_with_retries(
                    ex=ex,
                    kind=kind,
                    symbol=symbol,
                    ccxt_symbol=ccxt_symbol,
                    timeframe=timeframe,
                    since_ms=since_ms,
                    end_ms=end_ms,
                    limit=limit,
                    attempts=args.retry_attempts,
                    retry_sleep_sec=args.retry_sleep_sec,
                )
                artifact = None
                if not df.empty:
                    symbol_dir = out_dir / _safe_symbol(symbol)
                    symbol_dir.mkdir(parents=True, exist_ok=True)
                    suffix = "snapshot" if kind in {"instrument", "ticker", "orderbook", "trades"} else args.timeframe
                    artifact = _write_frame(df, symbol_dir / f"{kind}_{suffix}.parquet", out_dir)
                record.update(
                    {
                        "status": "ok",
                        "rows": int(len(df)),
                        "artifact": artifact,
                        "first_ts": str(df.index.min()) if not df.empty else None,
                        "last_ts": str(df.index.max()) if not df.empty else None,
                        "elapsed_sec": round(time.time() - started, 3),
                    }
                )
                ok += 1
                logging.info("OK %s %s rows=%d (%d/%d)", symbol, kind, len(df), job_index, total_jobs)
            except Exception as exc:
                record.update({"status": "failed", "error": str(exc), "elapsed_sec": round(time.time() - started, 3)})
                failed += 1
                logging.warning("FAILED %s %s: %s", symbol, kind, exc)
            _append_jsonl(progress_path, record)
            _write_json(
                status_path,
                {
                    "download_type": "derivatives_structure",
                    "run_id": run_id,
                    "status": "running",
                    "last_record": record,
                    "job_index": job_index,
                    "total_jobs": total_jobs,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            time.sleep(max(0.0, args.sleep_sec))

    artifacts = sorted(str(p.relative_to(out_dir)) for p in out_dir.glob("*/*.parquet"))
    manifest.update(
        {
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "status": "completed",
            "artifacts": artifacts,
            "summary": {"ok": ok, "failed": failed, "skipped_existing": skipped, "total_jobs": total_jobs},
        }
    )
    _write_json(manifest_path, manifest)
    _write_json(
        status_path,
        {
            "download_type": "derivatives_structure",
            "run_id": run_id,
            "status": "completed",
            "summary": manifest["summary"],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0 if failed == 0 else 1


def _create_okx() -> ccxt.okx:
    ex = ccxt.okx({"enableRateLimit": True, "options": {"defaultType": "swap", "fetchMarkets": {"types": ["swap"]}}})
    ex.load_markets()
    return ex


def _fetch_with_retries(
    ex,
    kind: str,
    symbol: str,
    ccxt_symbol: str,
    timeframe: str,
    since_ms: int,
    end_ms: int,
    limit: int,
    attempts: int,
    retry_sleep_sec: float,
) -> pd.DataFrame:
    last_exc: Exception | None = None
    for attempt in range(max(1, attempts)):
        try:
            if kind == "funding":
                return _fetch_funding(ex, symbol, ccxt_symbol, since_ms, end_ms, limit)
            if kind == "open_interest":
                return _fetch_open_interest(ex, symbol, ccxt_symbol, timeframe, since_ms, end_ms, limit)
            if kind == "long_short":
                return _fetch_long_short(ex, symbol, ccxt_symbol, timeframe, since_ms, end_ms, limit)
            if kind == "instrument":
                return _fetch_instrument(ex, symbol, ccxt_symbol)
            if kind == "ticker":
                return _fetch_ticker(ex, symbol, ccxt_symbol)
            if kind == "orderbook":
                return _fetch_orderbook(ex, symbol, ccxt_symbol, limit)
            if kind == "trades":
                return _fetch_trades(ex, symbol, ccxt_symbol, since_ms, end_ms, limit)
            raise ValueError(f"unsupported kind: {kind}")
        except Exception as exc:
            last_exc = exc
            if _is_fast_fail(exc):
                break
            if attempt < attempts - 1:
                time.sleep(retry_sleep_sec * (attempt + 1))
    raise last_exc or RuntimeError("fetch failed")


def _fetch_funding(ex, symbol: str, ccxt_symbol: str, since_ms: int, end_ms: int, limit: int) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []
    cursor = since_ms
    while cursor <= end_ms:
        rates = ex.fetch_funding_rate_history(ccxt_symbol, since=cursor, limit=limit)
        if not rates:
            break
        last_ts = None
        for rate in rates:
            ts_ms = rate.get("timestamp")
            if ts_ms is None:
                continue
            last_ts = int(ts_ms)
            if ts_ms > end_ms:
                continue
            rows.append(
                {
                    "timestamp": _timestamp_from_ms(ts_ms),
                    "symbol": symbol,
                    "funding_rate": _as_float(rate.get("fundingRate") or rate.get("rate")),
                    "funding_time": _timestamp_from_ms(_info_value(rate, "fundingTime")),
                }
            )
        if last_ts is None or last_ts >= end_ms or len(rates) < limit:
            break
        cursor = last_ts + 1
        time.sleep(ex.rateLimit / 1000)
    return _indexed(rows)


def _fetch_open_interest(ex, symbol: str, ccxt_symbol: str, timeframe: str, since_ms: int, end_ms: int, limit: int) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []
    cursor_end = end_ms
    stats_timeframe = _stats_timeframe(timeframe)
    step_ms = _timeframe_ms(stats_timeframe) * max(1, limit)
    while cursor_end >= since_ms:
        cursor_begin = max(since_ms, cursor_end - step_ms)
        data = ex.fetch_open_interest_history(
            ccxt_symbol,
            timeframe=stats_timeframe,
            since=cursor_begin,
            limit=limit,
            params={"until": cursor_end},
        )
        if not data:
            break
        min_ts = None
        for item in data:
            ts_ms = item.get("timestamp")
            if ts_ms is None:
                continue
            ts_ms = int(ts_ms)
            min_ts = ts_ms if min_ts is None else min(min_ts, ts_ms)
            if ts_ms < since_ms or ts_ms > end_ms:
                continue
            rows.append(
                {
                    "timestamp": _timestamp_from_ms(ts_ms),
                    "symbol": symbol,
                    "open_interest_amount": _as_float(item.get("openInterestAmount")),
                    "open_interest_value": _as_float(item.get("openInterestValue")),
                    "open_interest": _as_float(item.get("openInterest")),
                    "base_volume": _as_float(item.get("baseVolume")),
                    "quote_volume": _as_float(item.get("quoteVolume")),
                }
            )
        if min_ts is None or min_ts <= since_ms:
            break
        cursor_end = min_ts - 1
        time.sleep(ex.rateLimit / 1000)
    return _indexed(rows)


def _fetch_long_short(ex, symbol: str, ccxt_symbol: str, timeframe: str, since_ms: int, end_ms: int, limit: int) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []
    cursor = since_ms
    stats_timeframe = _stats_timeframe(timeframe)
    while cursor <= end_ms:
        data = ex.fetch_long_short_ratio_history(ccxt_symbol, timeframe=stats_timeframe, since=cursor, limit=limit)
        if not data:
            break
        last_ts = None
        for item in data:
            ts_ms = item.get("timestamp")
            if ts_ms is None:
                continue
            last_ts = int(ts_ms)
            if ts_ms > end_ms:
                continue
            rows.append(
                {
                    "timestamp": _timestamp_from_ms(ts_ms),
                    "symbol": symbol,
                    "long_short_ratio": _as_float(item.get("longShortRatio")),
                }
            )
        if last_ts is None or last_ts >= end_ms or len(data) < limit:
            break
        cursor = last_ts + 1
        time.sleep(ex.rateLimit / 1000)
    return _indexed(rows)


def _fetch_instrument(ex, symbol: str, ccxt_symbol: str) -> pd.DataFrame:
    market = ex.market(ccxt_symbol)
    info = market.get("info") or {}
    row = {
        "timestamp": pd.Timestamp.now(tz="UTC"),
        "symbol": symbol,
        "ccxt_symbol": ccxt_symbol,
        "inst_id": market.get("id"),
        "base": market.get("base"),
        "quote": market.get("quote"),
        "settle": market.get("settle"),
        "contract": bool(market.get("contract")),
        "linear": bool(market.get("linear")),
        "contract_size": _as_float(market.get("contractSize")),
        "min_amount": _nested_value(market, ("limits", "amount", "min")),
        "price_tick": _nested_value(market, ("precision", "price")),
        "amount_tick": _nested_value(market, ("precision", "amount")),
        "max_leverage": _as_float(info.get("lever")),
        "listed_time": _timestamp_from_ms(info.get("listTime")),
    }
    return pd.DataFrame([row]).set_index("timestamp").sort_index()


def _fetch_ticker(ex, symbol: str, ccxt_symbol: str) -> pd.DataFrame:
    ticker = ex.fetch_ticker(ccxt_symbol)
    info = ticker.get("info") or {}
    ts = _timestamp_from_ms(ticker.get("timestamp")) or pd.Timestamp.now(tz="UTC")
    row = {
        "timestamp": ts,
        "symbol": symbol,
        "bid": _as_float(ticker.get("bid")),
        "ask": _as_float(ticker.get("ask")),
        "last": _as_float(ticker.get("last")),
        "base_volume": _as_float(ticker.get("baseVolume") or info.get("volCcy24h")),
        "quote_volume": _as_float(ticker.get("quoteVolume") or info.get("volCcyQuote24h")),
        "open_24h": _as_float(info.get("open24h")),
        "high_24h": _as_float(info.get("high24h")),
        "low_24h": _as_float(info.get("low24h")),
    }
    mid = _mid(row.get("bid"), row.get("ask"))
    row["mid"] = mid
    row["spread"] = row["ask"] - row["bid"] if row.get("ask") is not None and row.get("bid") is not None else None
    row["spread_bps"] = (row["spread"] / mid * 10_000.0) if row.get("spread") is not None and mid else None
    return pd.DataFrame([row]).set_index("timestamp").sort_index()


def _fetch_orderbook(ex, symbol: str, ccxt_symbol: str, limit: int) -> pd.DataFrame:
    book = ex.fetch_order_book(ccxt_symbol, limit=limit)
    ts = _timestamp_from_ms(book.get("timestamp")) or pd.Timestamp.now(tz="UTC")
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    row: Dict[str, Any] = {"timestamp": ts, "symbol": symbol}
    bid_notional = 0.0
    ask_notional = 0.0
    max_levels = max(1, min(int(limit), 100))
    for i in range(max_levels):
        bid_px, bid_sz = _book_price_size(bids[i]) if i < len(bids) else (None, None)
        ask_px, ask_sz = _book_price_size(asks[i]) if i < len(asks) else (None, None)
        level = i + 1
        row[f"bid_px_{level}"] = bid_px
        row[f"bid_sz_{level}"] = bid_sz
        row[f"ask_px_{level}"] = ask_px
        row[f"ask_sz_{level}"] = ask_sz
        if bid_px is not None and bid_sz is not None:
            bid_notional += bid_px * bid_sz
        if ask_px is not None and ask_sz is not None:
            ask_notional += ask_px * ask_sz
    best_bid = row.get("bid_px_1")
    best_ask = row.get("ask_px_1")
    mid = _mid(best_bid, best_ask)
    spread = best_ask - best_bid if best_bid is not None and best_ask is not None else None
    total_depth = bid_notional + ask_notional
    row["mid"] = mid
    row["spread"] = spread
    row["spread_bps"] = (spread / mid * 10_000.0) if spread is not None and mid else None
    row["bid_notional_top"] = bid_notional
    row["ask_notional_top"] = ask_notional
    row["depth_notional_top"] = total_depth
    row["depth_imbalance"] = (bid_notional - ask_notional) / total_depth if total_depth else None
    return pd.DataFrame([row]).set_index("timestamp").sort_index()


def _fetch_trades(ex, symbol: str, ccxt_symbol: str, since_ms: int, end_ms: int, limit: int) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []
    trades = ex.fetch_trades(ccxt_symbol, since=since_ms, limit=max(1, min(int(limit), 1000)))
    for trade in trades or []:
        ts_ms = trade.get("timestamp")
        if ts_ms is None or ts_ms > end_ms:
            continue
        rows.append(
            {
                "timestamp": _timestamp_from_ms(ts_ms),
                "symbol": symbol,
                "id": trade.get("id"),
                "side": trade.get("side"),
                "price": _as_float(trade.get("price")),
                "amount": _as_float(trade.get("amount")),
                "cost": _as_float(trade.get("cost")),
            }
        )
    return _indexed(rows)


def _completed(path: Path) -> set[tuple[str, str, str]]:
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
            done.add((record.get("symbol"), record.get("kind"), record.get("timeframe")))
    return done


def _is_fast_fail(exc: Exception) -> bool:
    msg = str(exc)
    markers = ("Symbol not found", "does not have market symbol", "market not found", "does not have", "not supported")
    return any(marker in msg for marker in markers)


def _resolve_symbol(ex, symbol: str) -> str:
    if symbol in ex.markets:
        return symbol
    if ":" not in symbol:
        candidate = f"{symbol}:USDT"
        if candidate in ex.markets:
            return candidate
    raise ValueError(f"Symbol not found on OKX swap markets: {symbol}")


def _stats_timeframe(timeframe: str) -> str:
    return timeframe if timeframe in {"5m", "1h", "1d"} else "5m"


def _timeframe_ms(timeframe: str) -> int:
    if timeframe == "5m":
        return 5 * 60 * 1000
    if timeframe == "1h":
        return 60 * 60 * 1000
    if timeframe == "1d":
        return 24 * 60 * 60 * 1000
    return 5 * 60 * 1000


def _parse_symbols(raw: str) -> list[str]:
    return [s.strip() for s in raw.split(",") if s.strip()]


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _relpath(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))


def _append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    with path.open("a") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def _write_frame(df: pd.DataFrame, path: Path, root: Path) -> str:
    try:
        df.to_parquet(path)
        return str(path.relative_to(root))
    except Exception:
        fallback = path.with_suffix(".pkl")
        df.to_pickle(fallback)
        return str(fallback.relative_to(root))


def _indexed(rows: List[Dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df.dropna(subset=["timestamp"]).drop_duplicates(subset=["timestamp", "symbol"]).set_index("timestamp").sort_index()


def _safe_symbol(symbol: str) -> str:
    return symbol.replace("/", "_").replace(":", "_")


def _to_ms(value: str, end_of_day: bool = False) -> int:
    ts = pd.Timestamp(value, tz="UTC")
    if end_of_day and len(value) == 10:
        ts = ts + pd.Timedelta(days=1)
    now = pd.Timestamp.now(tz="UTC")
    if ts > now:
        ts = now
    return int(ts.timestamp() * 1000)


def _timestamp_from_ms(value) -> pd.Timestamp | None:
    if value in (None, ""):
        return None
    try:
        return pd.Timestamp(int(value), unit="ms", tz="UTC")
    except Exception:
        return None


def _as_float(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _nested_value(data: Dict[str, Any], keys: tuple[str, ...]) -> float | None:
    cur: Any = data
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return _as_float(cur)


def _book_price_size(level) -> tuple[float | None, float | None]:
    if not level or len(level) < 2:
        return None, None
    return _as_float(level[0]), _as_float(level[1])


def _mid(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None:
        return None
    return (bid + ask) / 2.0


def _info_value(item: Dict[str, Any], key: str):
    info = item.get("info") or {}
    return info.get(key)


if __name__ == "__main__":
    raise SystemExit(main())
