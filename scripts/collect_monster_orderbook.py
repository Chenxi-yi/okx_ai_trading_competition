#!/usr/bin/env python3
"""Collect candidate-only OKX orderbook snapshots for monster strategy research.

This script is append-only and never places orders. It reads the latest monster
watchlist, samples a bounded number of high-score candidates, and stores both
raw top-N levels and derived liquidity features.
"""

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

from data.fetcher import _resolve_symbol_alias  # noqa: E402

OUT_ROOT = ENGINE_DIR / "data" / "monster_events"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Collect monster candidate orderbook snapshots")
    p.add_argument("--watchlist-id", default=None, help="Defaults to newest monster watchlist.")
    p.add_argument("--dataset-id", default=None)
    p.add_argument("--top-n", type=int, default=30)
    p.add_argument("--candidate-only", action="store_true", help="Only sample rows with trade_candidate_flag=1.")
    p.add_argument("--min-score", type=float, default=0.75)
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--samples", type=int, default=1, help="Number of sampling rounds. Use 0 for continuous.")
    p.add_argument("--interval-sec", type=float, default=10.0)
    p.add_argument("--sleep-sec", type=float, default=0.15, help="Sleep between symbols.")
    p.add_argument("--log-level", default="INFO")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    dataset_id = args.dataset_id or f"monster_orderbook_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    out_dir = OUT_ROOT / dataset_id
    out_dir.mkdir(parents=True, exist_ok=True)
    progress_path = out_dir / "progress.jsonl"
    status_path = out_dir / "status.json"
    snapshots_path = out_dir / "orderbook_snapshots.jsonl"
    features_path = out_dir / "orderbook_features.jsonl"

    watchlist_id, watchlist = _load_watchlist(args.watchlist_id)
    symbols = _select_symbols(watchlist, args)
    if not symbols:
        raise SystemExit("No symbols selected from monster watchlist")

    ex = _exchange()
    manifest = {
        "dataset_id": dataset_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "watchlist_id": watchlist_id,
        "top_n": args.top_n,
        "candidate_only": args.candidate_only,
        "min_score": args.min_score,
        "limit": args.limit,
        "samples": args.samples,
        "interval_sec": args.interval_sec,
        "symbols": symbols,
        "artifacts": {
            "snapshots_jsonl": _relpath(snapshots_path),
            "features_jsonl": _relpath(features_path),
            "progress_jsonl": _relpath(progress_path),
        },
    }
    _write_json(out_dir / "manifest.json", manifest)

    sample_idx = 0
    ok = 0
    failed = 0
    while args.samples == 0 or sample_idx < args.samples:
        sample_idx += 1
        sample_started = datetime.now(timezone.utc).isoformat()
        _write_json(
            status_path,
            {
                **manifest,
                "status": "running",
                "sample_idx": sample_idx,
                "sample_started": sample_started,
                "ok": ok,
                "failed": failed,
            },
        )
        for i, symbol in enumerate(symbols, start=1):
            record = {"ts": datetime.now(timezone.utc).isoformat(), "sample_idx": sample_idx, "symbol": symbol}
            try:
                ccxt_symbol = _ccxt_symbol(ex, symbol)
                book = ex.fetch_order_book(ccxt_symbol, limit=args.limit)
                snapshot = _flatten_book(symbol, book, args.limit, sample_idx)
                features = _book_features(snapshot, [0.005, 0.01, 0.02])
                _append_jsonl(snapshots_path, snapshot)
                _append_jsonl(features_path, features)
                record.update({"status": "ok", "spread_bps": features.get("spread_bps"), "depth_1pct_usd": features.get("depth_1pct_usd")})
                ok += 1
                logging.info("OK %s spread=%s depth1=%s", symbol, features.get("spread_bps"), features.get("depth_1pct_usd"))
            except Exception as exc:
                record.update({"status": "failed", "error": str(exc)})
                failed += 1
                logging.warning("FAILED %s: %s", symbol, exc)
            _append_jsonl(progress_path, record)
            _write_json(
                status_path,
                {
                    **manifest,
                    "status": "running",
                    "sample_idx": sample_idx,
                    "current_symbol": symbol,
                    "current_index": i,
                    "ok": ok,
                    "failed": failed,
                    "last_record": record,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            time.sleep(max(args.sleep_sec, ex.rateLimit / 1000))
        if args.samples != 0 and sample_idx >= args.samples:
            break
        time.sleep(max(args.interval_sec, 1.0))

    _materialize_outputs(out_dir, snapshots_path, features_path)
    final = {**manifest, "status": "completed", "ok": ok, "failed": failed, "completed_at": datetime.now(timezone.utc).isoformat()}
    _write_json(status_path, final)
    _write_json(out_dir / "manifest.json", final)
    print(json.dumps(final, indent=2, sort_keys=True, default=str))
    return 0 if failed == 0 else 1


def _load_watchlist(watchlist_id: str | None) -> tuple[str, pd.DataFrame]:
    if watchlist_id:
        run_dir = OUT_ROOT / watchlist_id
    else:
        candidates = [
            p
            for p in OUT_ROOT.iterdir()
            if p.is_dir() and (p / "watchlist.parquet").exists()
        ]
        if not candidates:
            raise SystemExit("No monster watchlist found")
        run_dir = max(candidates, key=lambda p: (p / "watchlist.parquet").stat().st_mtime)
    path = run_dir / "watchlist.parquet"
    if not path.exists():
        raise SystemExit(f"watchlist.parquet not found under {run_dir}")
    return run_dir.name, pd.read_parquet(path)


def _select_symbols(watchlist: pd.DataFrame, args: argparse.Namespace) -> list[str]:
    df = watchlist.copy()
    if "monster_score_adj" in df:
        df = df[df["monster_score_adj"] >= args.min_score]
    if args.candidate_only and "trade_candidate_flag" in df:
        df = df[df["trade_candidate_flag"].astype(int) == 1]
    df = df.sort_values("monster_score_adj", ascending=False, na_position="last")
    symbols: list[str] = []
    for sym in df["symbol"].dropna().astype(str).tolist():
        if sym not in symbols:
            symbols.append(sym)
        if len(symbols) >= args.top_n:
            break
    return symbols


def _exchange() -> ccxt.okx:
    ex = ccxt.okx({"enableRateLimit": True, "options": {"defaultType": "swap", "fetchMarkets": {"types": ["swap"]}}})
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


def _flatten_book(symbol: str, book: dict[str, Any], limit: int, sample_idx: int) -> dict[str, Any]:
    ts = book.get("timestamp")
    row: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "exchange_ts": pd.to_datetime(ts, unit="ms", utc=True).isoformat() if ts else None,
        "sample_idx": sample_idx,
        "symbol": symbol,
        "nonce": book.get("nonce"),
    }
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    for i in range(limit):
        bid_px, bid_sz = _level(bids[i]) if i < len(bids) else (None, None)
        ask_px, ask_sz = _level(asks[i]) if i < len(asks) else (None, None)
        row[f"bid_px_{i + 1}"] = bid_px
        row[f"bid_sz_{i + 1}"] = bid_sz
        row[f"ask_px_{i + 1}"] = ask_px
        row[f"ask_sz_{i + 1}"] = ask_sz
    return row


def _book_features(row: dict[str, Any], pcts: list[float]) -> dict[str, Any]:
    bid = row.get("bid_px_1")
    ask = row.get("ask_px_1")
    mid = ((bid + ask) / 2.0) if bid and ask else None
    out: dict[str, Any] = {
        "ts": row["ts"],
        "exchange_ts": row.get("exchange_ts"),
        "sample_idx": row["sample_idx"],
        "symbol": row["symbol"],
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "spread_bps": ((ask - bid) / mid * 10000.0) if bid and ask and mid else None,
    }
    bid_top = _notional(row, "bid", 1)
    ask_top = _notional(row, "ask", 1)
    top_total = bid_top + ask_top
    out["top_imbalance"] = (bid_top - ask_top) / top_total if top_total else None
    for pct in pcts:
        bid_depth = _depth(row, "bid", mid, pct)
        ask_depth = _depth(row, "ask", mid, pct)
        total = bid_depth + ask_depth
        label = _pct_label(pct)
        out[f"bid_depth_{label}_usd"] = bid_depth
        out[f"ask_depth_{label}_usd"] = ask_depth
        out[f"depth_{label}_usd"] = total
        out[f"depth_imbalance_{label}"] = (bid_depth - ask_depth) / total if total else None
    if bid and ask:
        bid_sz = row.get("bid_sz_1") or 0.0
        ask_sz = row.get("ask_sz_1") or 0.0
        denom = bid_sz + ask_sz
        out["microprice_proxy"] = (ask * bid_sz + bid * ask_sz) / denom if denom else mid
        out["microprice_vs_mid_bps"] = ((out["microprice_proxy"] - mid) / mid * 10000.0) if mid else None
    return out


def _depth(row: dict[str, Any], side: str, mid: float | None, pct: float) -> float:
    if not mid:
        return 0.0
    total = 0.0
    i = 1
    while f"{side}_px_{i}" in row:
        px = row.get(f"{side}_px_{i}")
        sz = row.get(f"{side}_sz_{i}")
        if px is None or sz is None:
            i += 1
            continue
        if side == "bid" and px >= mid * (1.0 - pct):
            total += float(px) * float(sz)
        if side == "ask" and px <= mid * (1.0 + pct):
            total += float(px) * float(sz)
        i += 1
    return total


def _notional(row: dict[str, Any], side: str, level: int) -> float:
    px = row.get(f"{side}_px_{level}")
    sz = row.get(f"{side}_sz_{level}")
    return float(px) * float(sz) if px is not None and sz is not None else 0.0


def _pct_label(pct: float) -> str:
    bps = int(round(pct * 10000))
    if bps % 100 == 0:
        return f"{bps // 100}pct"
    return f"{bps}bps"


def _level(level: list[Any]) -> tuple[float | None, float | None]:
    try:
        return float(level[0]), float(level[1])
    except Exception:
        return None, None


def _materialize_outputs(out_dir: Path, snapshots_path: Path, features_path: Path) -> None:
    for source, stem in [(snapshots_path, "orderbook_snapshots"), (features_path, "orderbook_features")]:
        rows = []
        if source.exists():
            for line in source.read_text().splitlines():
                if line.strip():
                    rows.append(json.loads(line))
        if not rows:
            continue
        df = pd.DataFrame(rows)
        df.to_csv(out_dir / f"{stem}.csv", index=False)
        df.to_parquet(out_dir / f"{stem}.parquet")


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    with path.open("a") as fh:
        fh.write(json.dumps(payload, sort_keys=True, default=str) + "\n")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _relpath(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


if __name__ == "__main__":
    raise SystemExit(main())
