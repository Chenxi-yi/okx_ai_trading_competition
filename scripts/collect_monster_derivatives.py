#!/usr/bin/env python3
"""Collect live derivatives structure snapshots for monster candidates.

This is a read-only, append-only collector. It reads the latest monster
watchlist, samples high-score candidates, and stores current/near-current
funding, open interest, and long/short ratio observations.
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
    p = argparse.ArgumentParser(description="Collect live derivatives snapshots for monster candidates")
    p.add_argument("--watchlist-id", default=None, help="Defaults to newest monster watchlist.")
    p.add_argument("--dataset-id", default=None)
    p.add_argument("--top-n", type=int, default=30)
    p.add_argument("--candidate-only", action="store_true")
    p.add_argument("--min-score", type=float, default=0.75)
    p.add_argument("--timeframe", default="5m", choices=["5m", "1h", "1d"])
    p.add_argument("--samples", type=int, default=1, help="Number of sampling rounds. Use 0 for continuous.")
    p.add_argument("--interval-sec", type=float, default=60.0)
    p.add_argument("--sleep-sec", type=float, default=0.2, help="Sleep between symbols.")
    p.add_argument("--log-level", default="INFO")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    dataset_id = args.dataset_id or f"monster_derivatives_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    out_dir = OUT_ROOT / dataset_id
    out_dir.mkdir(parents=True, exist_ok=True)
    progress_path = out_dir / "progress.jsonl"
    features_path = out_dir / "derivatives_features.jsonl"
    status_path = out_dir / "status.json"

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
        "timeframe": args.timeframe,
        "samples": args.samples,
        "interval_sec": args.interval_sec,
        "symbols": symbols,
        "artifacts": {
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
        _write_json(
            status_path,
            {
                **manifest,
                "status": "running",
                "sample_idx": sample_idx,
                "ok": ok,
                "failed": failed,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        for i, symbol in enumerate(symbols, start=1):
            started = time.time()
            record = {"ts": datetime.now(timezone.utc).isoformat(), "sample_idx": sample_idx, "symbol": symbol}
            try:
                ccxt_symbol = _ccxt_symbol(ex, symbol)
                features = _derivatives_features(ex, symbol, ccxt_symbol, args.timeframe, sample_idx)
                _append_jsonl(features_path, features)
                record.update(
                    {
                        "status": "ok",
                        "elapsed_sec": round(time.time() - started, 3),
                        "open_interest_value": features.get("open_interest_value"),
                        "funding_rate": features.get("funding_rate"),
                        "long_short_ratio": features.get("long_short_ratio"),
                    }
                )
                ok += 1
                logging.info(
                    "OK %s oi_value=%s funding=%s lsr=%s",
                    symbol,
                    features.get("open_interest_value"),
                    features.get("funding_rate"),
                    features.get("long_short_ratio"),
                )
            except Exception as exc:
                record.update({"status": "failed", "error": str(exc), "elapsed_sec": round(time.time() - started, 3)})
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

    _materialize_outputs(out_dir, features_path)
    final = {
        **manifest,
        "status": "completed",
        "ok": ok,
        "failed": failed,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
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
    score_col = "monster_score_adj" if "monster_score_adj" in df else None
    if score_col:
        df = df.sort_values(score_col, ascending=False, na_position="last")
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


def _derivatives_features(ex: ccxt.Exchange, symbol: str, ccxt_symbol: str, timeframe: str, sample_idx: int) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    out: dict[str, Any] = {"ts": now, "sample_idx": sample_idx, "symbol": symbol, "ccxt_symbol": ccxt_symbol}
    out.update(_current_open_interest(ex, ccxt_symbol))
    out.update(_latest_open_interest_history(ex, ccxt_symbol, timeframe))
    out.update(_current_funding(ex, ccxt_symbol))
    out.update(_latest_long_short_ratio(ex, ccxt_symbol, timeframe))
    return out


def _current_open_interest(ex: ccxt.Exchange, ccxt_symbol: str) -> dict[str, Any]:
    try:
        item = ex.fetch_open_interest(ccxt_symbol)
    except Exception as exc:
        return {"open_interest_error": str(exc)}
    info = item.get("info") or {}
    return {
        "open_interest_ts": _timestamp_from_ms(item.get("timestamp") or info.get("ts")),
        "open_interest": _as_float(item.get("openInterest") or item.get("openInterestAmount") or info.get("oi")),
        "open_interest_amount": _as_float(item.get("openInterestAmount") or info.get("oi")),
        "open_interest_value": _as_float(item.get("openInterestValue") or info.get("oiUsd") or info.get("oiCcy")),
    }


def _latest_open_interest_history(ex: ccxt.Exchange, ccxt_symbol: str, timeframe: str) -> dict[str, Any]:
    try:
        rows = ex.fetch_open_interest_history(ccxt_symbol, timeframe=timeframe, limit=2)
    except Exception as exc:
        return {"open_interest_history_error": str(exc)}
    if not rows:
        return {}
    last = rows[-1]
    return {
        "open_interest_hist_ts": _timestamp_from_ms(last.get("timestamp")),
        "open_interest_hist": _as_float(last.get("openInterest")),
        "open_interest_hist_amount": _as_float(last.get("openInterestAmount")),
        "open_interest_hist_value": _as_float(last.get("openInterestValue")),
    }


def _current_funding(ex: ccxt.Exchange, ccxt_symbol: str) -> dict[str, Any]:
    try:
        item = ex.fetch_funding_rate(ccxt_symbol)
    except Exception as exc:
        return {"funding_error": str(exc)}
    return {
        "funding_ts": _timestamp_from_ms(item.get("timestamp")),
        "funding_rate": _as_float(item.get("fundingRate")),
        "next_funding_rate": _as_float(item.get("nextFundingRate")),
        "funding_datetime": item.get("datetime"),
    }


def _latest_long_short_ratio(ex: ccxt.Exchange, ccxt_symbol: str, timeframe: str) -> dict[str, Any]:
    try:
        rows = ex.fetch_long_short_ratio_history(ccxt_symbol, timeframe=timeframe, limit=2)
    except Exception as exc:
        return {"long_short_error": str(exc)}
    if not rows:
        return {}
    last = rows[-1]
    return {
        "long_short_ts": _timestamp_from_ms(last.get("timestamp")),
        "long_short_ratio": _as_float(last.get("longShortRatio")),
    }


def _materialize_outputs(out_dir: Path, features_path: Path) -> None:
    rows = []
    if features_path.exists():
        for line in features_path.read_text().splitlines():
            if line.strip():
                rows.append(json.loads(line))
    if not rows:
        return
    df = pd.DataFrame(rows)
    df.to_csv(out_dir / "derivatives_features.csv", index=False)
    df.to_parquet(out_dir / "derivatives_features.parquet")


def _timestamp_from_ms(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        return pd.Timestamp(int(value), unit="ms", tz="UTC").isoformat()
    except Exception:
        return None


def _as_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except Exception:
        return None


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
