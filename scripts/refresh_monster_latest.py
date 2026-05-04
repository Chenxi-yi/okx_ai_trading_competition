#!/usr/bin/env python3
"""Refresh recent 5m OHLCV plus ticker/orderbook snapshots for monster scoring."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import ccxt
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
ENGINE_DIR = ROOT / "engine"
sys.path.insert(0, str(ENGINE_DIR))

from config.settings import DATA_DIR  # noqa: E402
from data.fetcher import _resolve_symbol_alias  # noqa: E402

DEFAULT_HISTORY_MANIFEST = (
    ENGINE_DIR / "data" / "training_history" / "train_hist_134_5m_20240101_20260424" / "manifest.json"
)
OUT_ROOT = ENGINE_DIR / "data" / "monster_events"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Refresh latest OKX public data for monster watchlist scoring")
    p.add_argument("--history-manifest", default=str(DEFAULT_HISTORY_MANIFEST))
    p.add_argument("--symbols", default=None, help="Comma-separated ccxt symbols. Defaults to manifest symbols.")
    p.add_argument("--dataset-id", default=None)
    p.add_argument("--timeframe", default="5m")
    p.add_argument("--lookback-days", type=float, default=3.0)
    p.add_argument("--orderbook-limit", type=int, default=20)
    p.add_argument("--sleep-sec", type=float, default=0.25)
    p.add_argument("--log-level", default="INFO")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    symbols = _symbols(args)
    dataset_id = args.dataset_id or f"monster_latest_refresh_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    out_dir = OUT_ROOT / dataset_id
    out_dir.mkdir(parents=True, exist_ok=True)
    status_path = out_dir / "status.json"
    progress_path = out_dir / "progress.jsonl"

    ex = _exchange()
    end = pd.Timestamp.utcnow().ceil(_pandas_freq(args.timeframe))
    start = end - pd.Timedelta(days=args.lookback_days)
    rows: list[dict[str, Any]] = []
    ok = 0
    failed = 0

    for i, symbol in enumerate(symbols, start=1):
        _write_json(status_path, _status(dataset_id, "running", symbol, i, len(symbols)))
        record = {"symbol": symbol, "status": "running", "started_at": pd.Timestamp.utcnow().isoformat()}
        try:
            ccxt_symbol = _ccxt_symbol(ex, symbol)
            recent = _fetch_recent_ohlcv(ex, symbol, ccxt_symbol, args.timeframe, start, end)
            merged_rows, first_ts, last_ts = _merge_cache(symbol, args.timeframe, recent)
            bar_age_hours = float((pd.Timestamp.utcnow() - pd.Timestamp(last_ts)) / pd.Timedelta(hours=1)) if last_ts else None
            snapshot = _fetch_snapshot(ex, symbol, ccxt_symbol, args.orderbook_limit)
            record.update(
                {
                    "status": "ok",
                    "recent_rows": int(len(recent)),
                    "cache_rows": int(merged_rows),
                    "first_ts": first_ts,
                    "last_ts": last_ts,
                    "bar_age_hours": bar_age_hours,
                    **snapshot,
                }
            )
            rows.append(record.copy())
            ok += 1
            logging.info("OK %s recent=%d cache=%d", symbol, len(recent), merged_rows)
        except Exception as exc:
            record.update({"status": "failed", "error": str(exc)})
            rows.append(record.copy())
            failed += 1
            logging.warning("FAILED %s: %s", symbol, exc)
        _append_jsonl(progress_path, record)
        _write_json(status_path, _status(dataset_id, "running", symbol, i, len(symbols), record))
        time.sleep(max(args.sleep_sec, ex.rateLimit / 1000))

    snapshot_df = pd.DataFrame(rows)
    snapshot_path = out_dir / "market_snapshot.csv"
    snapshot_parquet = out_dir / "market_snapshot.parquet"
    snapshot_df.to_csv(snapshot_path, index=False)
    snapshot_df.to_parquet(snapshot_parquet)
    manifest = {
        "dataset_id": dataset_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "symbols": len(symbols),
        "ok": ok,
        "failed": failed,
        "timeframe": args.timeframe,
        "lookback_days": args.lookback_days,
        "history_manifest": _relpath(Path(args.history_manifest)),
        "artifacts": {
            "market_snapshot_csv": _relpath(snapshot_path),
            "market_snapshot_parquet": _relpath(snapshot_parquet),
            "progress_jsonl": _relpath(progress_path),
        },
    }
    _write_json(out_dir / "manifest.json", manifest)
    _write_json(status_path, {"dataset_id": dataset_id, "status": "completed", "ok": ok, "failed": failed})
    print(json.dumps(manifest, indent=2, sort_keys=True, default=str))
    if not snapshot_df.empty:
        cols = [c for c in ["symbol", "status", "last_ts", "bar_age_hours", "quote_volume_24h", "spread_bps", "depth_1pct_usd"] if c in snapshot_df.columns]
        print(snapshot_df[cols].sort_values("quote_volume_24h", ascending=False, na_position="last").head(30).to_string(index=False))
    return 0 if failed == 0 else 1


def _symbols(args: argparse.Namespace) -> list[str]:
    if args.symbols:
        return [s.strip() for s in args.symbols.split(",") if s.strip()]
    manifest = json.loads(Path(args.history_manifest).read_text())
    return list(manifest["symbols"])


def _exchange() -> ccxt.okx:
    ex = ccxt.okx(
        {
            "enableRateLimit": True,
            "options": {"defaultType": "swap", "fetchMarkets": {"types": ["swap"]}},
        }
    )
    last_exc: Exception | None = None
    for attempt in range(5):
        try:
            ex.load_markets()
            return ex
        except Exception as exc:
            last_exc = exc
            time.sleep(2.0 * (attempt + 1))
    raise last_exc or RuntimeError("failed to load OKX swap markets")
    return ex


def _ccxt_symbol(ex: ccxt.Exchange, symbol: str) -> str:
    source = _resolve_symbol_alias(symbol, "futures")
    candidate = f"{source}:USDT" if ":" not in source else source
    if candidate in ex.markets:
        return candidate
    if source in ex.markets:
        return source
    raise ValueError(f"symbol not found in OKX swap markets: {symbol}")


def _fetch_recent_ohlcv(ex: ccxt.Exchange, symbol: str, ccxt_symbol: str, timeframe: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    since_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    rows: list[list[Any]] = []
    while True:
        bars = ex.fetch_ohlcv(ccxt_symbol, timeframe=timeframe, since=since_ms, limit=1000)
        if not bars:
            break
        rows.extend(bars)
        last_ts = int(bars[-1][0])
        if last_ts >= end_ms:
            break
        if last_ts < since_ms:
            raise ValueError(f"OHLCV cursor did not advance for {symbol}: last_ts={last_ts} since={since_ms}")
        since_ms = last_ts + 1
        time.sleep(max(ex.rateLimit / 1000, 0.2))
    if not rows:
        raise ValueError(f"No recent OHLCV returned for {symbol}")
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True).dt.floor(_pandas_freq(timeframe))
    df = df.set_index("timestamp").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    df = df[df.index <= end]
    df["funding_rate"] = 0.0
    return df.astype(float)


def _merge_cache(symbol: str, timeframe: str, recent: pd.DataFrame) -> tuple[int, str | None, str | None]:
    safe = symbol.replace("/", "_")
    parquet_path = DATA_DIR / f"{safe}_futures_{timeframe}.parquet"
    pickle_path = DATA_DIR / f"{safe}_futures_{timeframe}.pkl"
    existing = None
    if parquet_path.exists():
        existing = pd.read_parquet(parquet_path)
    elif pickle_path.exists():
        existing = pd.read_pickle(pickle_path)
    merged = recent if existing is None or existing.empty else pd.concat([existing, recent]).sort_index()
    if merged.index.tz is None:
        merged.index = merged.index.tz_localize("UTC")
    else:
        merged.index = merged.index.tz_convert("UTC")
    merged = merged[~merged.index.duplicated(keep="last")]
    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    merged.to_parquet(parquet_path)
    return int(len(merged)), str(merged.index.min()) if len(merged) else None, str(merged.index.max()) if len(merged) else None


def _fetch_snapshot(ex: ccxt.Exchange, symbol: str, ccxt_symbol: str, orderbook_limit: int) -> dict[str, Any]:
    ticker = ex.fetch_ticker(ccxt_symbol)
    orderbook = ex.fetch_order_book(ccxt_symbol, limit=orderbook_limit)
    bid = _as_float(ticker.get("bid"))
    ask = _as_float(ticker.get("ask"))
    last = _as_float(ticker.get("last"))
    mid = ((bid + ask) / 2.0) if bid and ask else last
    spread_bps = ((ask - bid) / mid * 10000.0) if bid and ask and mid else None
    quote_volume = _quote_volume(ticker)
    depth_1pct = _depth_usd(orderbook, mid, pct=0.01)
    return {
        "ticker_ts": str(pd.to_datetime(ticker.get("timestamp"), unit="ms", utc=True)) if ticker.get("timestamp") else None,
        "last": last,
        "bid": bid,
        "ask": ask,
        "spread_bps": spread_bps,
        "quote_volume_24h": quote_volume,
        "depth_1pct_usd": depth_1pct,
    }


def _depth_usd(orderbook: dict[str, Any], mid: float | None, pct: float) -> float | None:
    if not mid:
        return None
    total = 0.0
    for price, amount, *_ in orderbook.get("bids", []):
        price = float(price)
        if price < mid * (1.0 - pct):
            continue
        total += price * float(amount)
    for price, amount, *_ in orderbook.get("asks", []):
        price = float(price)
        if price > mid * (1.0 + pct):
            continue
        total += price * float(amount)
    return total


def _quote_volume(ticker: dict[str, Any]) -> float | None:
    if ticker.get("quoteVolume"):
        return _as_float(ticker.get("quoteVolume"))
    info = ticker.get("info") or {}
    for key in ("volCcyQuote24h", "volCcyQuote", "quoteVolume"):
        if info.get(key):
            return _as_float(info.get(key))
    base = _as_float(ticker.get("baseVolume") or info.get("volCcy24h"))
    last = _as_float(ticker.get("last") or info.get("last"))
    return base * last if base is not None and last is not None else None


def _as_float(value: Any) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except Exception:
        return None


def _status(dataset_id: str, status: str, symbol: str, index: int, total: int, last_record: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "dataset_id": dataset_id,
        "status": status,
        "current_symbol": symbol,
        "index": index,
        "total": total,
        "last_record": last_record,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    with path.open("a") as fh:
        fh.write(json.dumps(payload, sort_keys=True, default=str) + "\n")


def _relpath(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def _pandas_freq(timeframe: str) -> str:
    if timeframe.endswith("m"):
        return timeframe[:-1] + "min"
    return timeframe


if __name__ == "__main__":
    raise SystemExit(main())
