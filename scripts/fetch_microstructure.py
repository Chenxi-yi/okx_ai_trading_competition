#!/usr/bin/env python3
"""Fetch OKX public microstructure datasets."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parents[1]
ENGINE_DIR = ROOT / "engine"
sys.path.insert(0, str(ENGINE_DIR))

from data.microstructure import fetch_microstructure_dataset


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Fetch public OKX microstructure and derivatives data")
    p.add_argument("--symbols", default="BTC/USDT,ETH/USDT,SOL/USDT", help="Comma-separated ccxt symbols")
    p.add_argument("--dataset-id", default=None)
    p.add_argument("--start", default=None, help="UTC start timestamp/date for historical endpoints")
    p.add_argument("--end", default=None, help="UTC end timestamp/date for historical endpoints")
    p.add_argument("--timeframe", default="5m", help="Timeframe for OI/long-short history, e.g. 5m, 1h, 1d")
    p.add_argument(
        "--kinds",
        default="ticker,instrument,ohlcv,funding,open_interest,long_short,trades,orderbook",
        help="Comma-separated: ticker,instrument,ohlcv,funding,open_interest,long_short,trades,orderbook",
    )
    p.add_argument("--orderbook-limit", type=int, default=10)
    p.add_argument("--orderbook-samples", type=int, default=1)
    p.add_argument("--orderbook-interval-sec", type=float, default=1.0)
    p.add_argument("--trades-limit", type=int, default=1000)
    p.add_argument("--derivatives-limit", type=int, default=100)
    p.add_argument("--log-level", default="INFO")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    symbols: List[str] = [s.strip() for s in args.symbols.split(",") if s.strip()]
    kinds: List[str] = [k.strip() for k in args.kinds.split(",") if k.strip()]
    dataset_id = args.dataset_id or f"micro_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    manifest = fetch_microstructure_dataset(
        symbols=symbols,
        dataset_id=dataset_id,
        start=args.start,
        end=args.end,
        kinds=kinds,
        timeframe=args.timeframe,
        orderbook_limit=args.orderbook_limit,
        orderbook_samples=args.orderbook_samples,
        orderbook_interval_sec=args.orderbook_interval_sec,
        trades_limit=args.trades_limit,
        derivatives_limit=args.derivatives_limit,
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    failed = [r for r in manifest["results"] if r["status"] != "ok"]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
