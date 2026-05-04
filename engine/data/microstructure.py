"""Public OKX microstructure and derivatives data collector."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import ccxt
import pandas as pd

from config.settings import DATA_DIR

logger = logging.getLogger(__name__)

MICROSTRUCTURE_DIR = DATA_DIR.parent / "microstructure"


@dataclass
class FetchResult:
    kind: str
    symbol: str
    rows: int
    artifact: Optional[str]
    status: str
    error: Optional[str] = None

    def to_dict(self) -> Dict:
        return asdict(self)


def create_public_okx(default_type: str = "swap") -> ccxt.okx:
    ex = ccxt.okx(
        {
            "enableRateLimit": True,
            "options": {
                "defaultType": default_type,
                "fetchMarkets": {"types": ["swap"]} if default_type == "swap" else None,
            },
        }
    )
    ex.load_markets()
    return ex


def fetch_microstructure_dataset(
    symbols: Iterable[str],
    dataset_id: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    kinds: Iterable[str] = ("ticker", "instrument", "ohlcv", "funding", "open_interest", "long_short", "trades", "orderbook"),
    timeframe: str = "5m",
    orderbook_limit: int = 10,
    orderbook_samples: int = 1,
    orderbook_interval_sec: float = 1.0,
    trades_limit: int = 1000,
    derivatives_limit: int = 100,
) -> Dict:
    """
    Fetch public market microstructure data and write a versioned dataset.

    This is read-only market data. It does not use order endpoints.
    """
    symbols = [s.strip() for s in symbols if s and s.strip()]
    kinds = tuple(k.strip() for k in kinds if k and k.strip())
    out_dir = MICROSTRUCTURE_DIR / dataset_id
    out_dir.mkdir(parents=True, exist_ok=True)

    ex = create_public_okx(default_type="swap")
    since_ms = _to_ms(start) if start else None
    end_ms = _to_ms(end) if end else None
    results: List[FetchResult] = []

    for symbol in symbols:
        ccxt_symbol = _resolve_symbol(ex, symbol)
        safe_symbol = _safe_symbol(symbol)
        symbol_dir = out_dir / safe_symbol
        symbol_dir.mkdir(parents=True, exist_ok=True)

        for kind in kinds:
            try:
                df = _fetch_kind(
                    ex,
                    kind=kind,
                    symbol=symbol,
                    ccxt_symbol=ccxt_symbol,
                    since_ms=since_ms,
                    end_ms=end_ms,
                    timeframe=timeframe,
                    orderbook_limit=orderbook_limit,
                    orderbook_samples=orderbook_samples,
                    orderbook_interval_sec=orderbook_interval_sec,
                    trades_limit=trades_limit,
                    derivatives_limit=derivatives_limit,
                )
                artifact = None
                if df is not None and not df.empty:
                    artifact = _write_frame(df, symbol_dir / f"{kind}.parquet", out_dir)
                results.append(FetchResult(kind, symbol, int(0 if df is None else len(df)), artifact, "ok").to_dict())
            except Exception as exc:
                logger.warning("Microstructure fetch failed: %s %s: %s", symbol, kind, exc)
                results.append(FetchResult(kind, symbol, 0, None, "failed", str(exc)).to_dict())
            time.sleep(ex.rateLimit / 1000)

    manifest = _build_manifest(
        out_dir=out_dir,
        dataset_id=dataset_id,
        symbols=symbols,
        kinds=kinds,
        start=start,
        end=end,
        timeframe=timeframe,
        orderbook_limit=orderbook_limit,
        orderbook_samples=orderbook_samples,
        results=results,
    )
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return manifest


def _fetch_kind(
    ex,
    kind: str,
    symbol: str,
    ccxt_symbol: str,
    since_ms: Optional[int],
    end_ms: Optional[int],
    timeframe: str,
    orderbook_limit: int,
    orderbook_samples: int,
    orderbook_interval_sec: float,
    trades_limit: int,
    derivatives_limit: int,
) -> pd.DataFrame:
    if kind == "ticker":
        return _fetch_ticker(ex, symbol, ccxt_symbol)
    if kind == "instrument":
        return _fetch_instrument(ex, symbol, ccxt_symbol)
    if kind == "ohlcv":
        return _fetch_ohlcv(ex, symbol, ccxt_symbol, timeframe, since_ms, end_ms)
    if kind == "funding":
        return _fetch_funding(ex, symbol, ccxt_symbol, since_ms, end_ms, derivatives_limit)
    if kind == "open_interest":
        return _fetch_open_interest_history(ex, symbol, ccxt_symbol, timeframe, since_ms, end_ms, derivatives_limit)
    if kind == "long_short":
        return _fetch_long_short_history(ex, symbol, ccxt_symbol, timeframe, since_ms, derivatives_limit)
    if kind == "trades":
        return _fetch_trades(ex, symbol, ccxt_symbol, since_ms, end_ms, trades_limit)
    if kind == "orderbook":
        return _fetch_orderbook_snapshots(ex, symbol, ccxt_symbol, orderbook_limit, orderbook_samples, orderbook_interval_sec)
    raise ValueError(f"Unknown microstructure kind: {kind}")


def _fetch_ticker(ex, symbol: str, ccxt_symbol: str) -> pd.DataFrame:
    ticker = ex.fetch_ticker(ccxt_symbol)
    ts = _timestamp_from_ms(ticker.get("timestamp")) or _now_utc()
    row = {
        "timestamp": ts,
        "symbol": symbol,
        "bid": _as_float(ticker.get("bid")),
        "ask": _as_float(ticker.get("ask")),
        "last": _as_float(ticker.get("last")),
        "quote_volume": _as_float(ticker.get("quoteVolume")),
        "base_volume": _as_float(ticker.get("baseVolume")),
        "percentage": _as_float(ticker.get("percentage")),
    }
    return pd.DataFrame([row]).set_index("timestamp")


def _fetch_instrument(ex, symbol: str, ccxt_symbol: str) -> pd.DataFrame:
    market = ex.market(ccxt_symbol)
    info = market.get("info", {})
    row = {
        "timestamp": _now_utc(),
        "symbol": symbol,
        "ccxt_symbol": ccxt_symbol,
        "id": market.get("id"),
        "base": market.get("base"),
        "quote": market.get("quote"),
        "settle": market.get("settle"),
        "contract": bool(market.get("contract")),
        "linear": bool(market.get("linear")),
        "contract_size": _as_float(market.get("contractSize")),
        "min_amount": _nested_float(market, ["limits", "amount", "min"]),
        "price_tick": _nested_float(market, ["precision", "price"]),
        "amount_tick": _nested_float(market, ["precision", "amount"]),
        "max_leverage": _as_float(info.get("lever")),
        "listed_time": _timestamp_from_ms(info.get("listTime")),
    }
    return pd.DataFrame([row]).set_index("timestamp")


def _fetch_ohlcv(
    ex,
    symbol: str,
    ccxt_symbol: str,
    timeframe: str,
    since_ms: Optional[int],
    end_ms: Optional[int],
) -> pd.DataFrame:
    rows = []
    cursor = since_ms
    while True:
        bars = ex.fetch_ohlcv(ccxt_symbol, timeframe=timeframe, since=cursor, limit=1000)
        if not bars:
            break
        for ts_ms, open_, high, low, close, volume in bars:
            if end_ms and ts_ms > end_ms:
                continue
            rows.append(
                {
                    "timestamp": _timestamp_from_ms(ts_ms),
                    "symbol": symbol,
                    "open": _as_float(open_),
                    "high": _as_float(high),
                    "low": _as_float(low),
                    "close": _as_float(close),
                    "volume": _as_float(volume),
                }
            )
        last_ts = bars[-1][0]
        if not since_ms or (end_ms and last_ts >= end_ms) or len(bars) < 1000:
            break
        cursor = int(last_ts) + 1
        time.sleep(ex.rateLimit / 1000)
    return _indexed(rows)


def _fetch_funding(
    ex,
    symbol: str,
    ccxt_symbol: str,
    since_ms: Optional[int],
    end_ms: Optional[int],
    limit: int,
) -> pd.DataFrame:
    rows: List[Dict] = []
    cursor = since_ms
    while True:
        rates = ex.fetch_funding_rate_history(ccxt_symbol, since=cursor, limit=limit)
        if not rates:
            break
        for rate in rates:
            ts_ms = rate.get("timestamp")
            if end_ms and ts_ms and ts_ms > end_ms:
                continue
            rows.append(
                {
                    "timestamp": _timestamp_from_ms(ts_ms),
                    "symbol": symbol,
                    "funding_rate": _as_float(rate.get("fundingRate") or rate.get("rate")),
                    "funding_time": _timestamp_from_ms(_info_value(rate, "fundingTime")),
                }
            )
        last_ts = rates[-1].get("timestamp")
        if not since_ms or not last_ts or (end_ms and last_ts >= end_ms) or len(rates) < limit:
            break
        cursor = int(last_ts) + 1
        time.sleep(ex.rateLimit / 1000)
    return _indexed(rows)


def _fetch_open_interest_history(
    ex,
    symbol: str,
    ccxt_symbol: str,
    timeframe: str,
    since_ms: Optional[int],
    end_ms: Optional[int],
    limit: int,
) -> pd.DataFrame:
    stats_timeframe = _okx_stats_timeframe(timeframe)
    data = ex.fetch_open_interest_history(ccxt_symbol, timeframe=stats_timeframe, since=since_ms, limit=limit)
    rows = []
    for item in data or []:
        ts_ms = item.get("timestamp")
        if end_ms and ts_ms and ts_ms > end_ms:
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
    return _indexed(rows)


def _fetch_long_short_history(
    ex,
    symbol: str,
    ccxt_symbol: str,
    timeframe: str,
    since_ms: Optional[int],
    limit: int,
) -> pd.DataFrame:
    stats_timeframe = _okx_stats_timeframe(timeframe)
    data = ex.fetch_long_short_ratio_history(ccxt_symbol, timeframe=stats_timeframe, since=since_ms, limit=limit)
    rows = []
    for item in data or []:
        rows.append(
            {
                "timestamp": _timestamp_from_ms(item.get("timestamp")),
                "symbol": symbol,
                "long_short_ratio": _as_float(item.get("longShortRatio")),
            }
        )
    return _indexed(rows)


def _fetch_trades(
    ex,
    symbol: str,
    ccxt_symbol: str,
    since_ms: Optional[int],
    end_ms: Optional[int],
    limit: int,
) -> pd.DataFrame:
    data = ex.fetch_trades(ccxt_symbol, since=since_ms, limit=limit)
    rows = []
    for trade in data or []:
        ts_ms = trade.get("timestamp")
        if end_ms and ts_ms and ts_ms > end_ms:
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


def _fetch_orderbook_snapshots(
    ex,
    symbol: str,
    ccxt_symbol: str,
    limit: int,
    samples: int,
    interval_sec: float,
) -> pd.DataFrame:
    rows = []
    for i in range(max(1, samples)):
        book = ex.fetch_order_book(ccxt_symbol, limit=limit)
        rows.append(_flatten_orderbook(symbol, book, limit))
        if i < samples - 1:
            time.sleep(max(0.0, interval_sec))
    return _indexed(rows)


def _flatten_orderbook(symbol: str, book: Dict, limit: int) -> Dict:
    ts = _timestamp_from_ms(book.get("timestamp")) or _now_utc()
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    row: Dict = {"timestamp": ts, "symbol": symbol}
    bid_notional = 0.0
    ask_notional = 0.0
    for i in range(limit):
        bid_px, bid_sz = _book_price_size(bids[i]) if i < len(bids) else (None, None)
        ask_px, ask_sz = _book_price_size(asks[i]) if i < len(asks) else (None, None)
        level = i + 1
        row[f"bid_px_{level}"] = _as_float(bid_px)
        row[f"bid_sz_{level}"] = _as_float(bid_sz)
        row[f"ask_px_{level}"] = _as_float(ask_px)
        row[f"ask_sz_{level}"] = _as_float(ask_sz)
        if bid_px and bid_sz:
            bid_notional += float(bid_px) * float(bid_sz)
        if ask_px and ask_sz:
            ask_notional += float(ask_px) * float(ask_sz)
    best_bid = row.get("bid_px_1")
    best_ask = row.get("ask_px_1")
    mid = (best_bid + best_ask) / 2.0 if best_bid and best_ask else None
    row["mid"] = mid
    row["spread"] = best_ask - best_bid if best_bid and best_ask else None
    row["spread_bps"] = (row["spread"] / mid * 10_000.0) if row["spread"] and mid else None
    total = bid_notional + ask_notional
    row["bid_notional_top"] = bid_notional
    row["ask_notional_top"] = ask_notional
    row["depth_imbalance"] = (bid_notional - ask_notional) / total if total else None
    return row


def _book_price_size(level) -> tuple[Optional[float], Optional[float]]:
    if not level or len(level) < 2:
        return None, None
    return _as_float(level[0]), _as_float(level[1])


def _resolve_symbol(ex, symbol: str) -> str:
    if symbol in ex.markets:
        return symbol
    if ":" not in symbol:
        candidate = f"{symbol}:USDT"
        if candidate in ex.markets:
            return candidate
    raise ValueError(f"Symbol not found on OKX swap markets: {symbol}")


def _okx_stats_timeframe(timeframe: str) -> str:
    if timeframe in {"5m", "1h", "1d"}:
        return timeframe
    return "5m"


def _write_frame(df: pd.DataFrame, path: Path, root: Path) -> str:
    try:
        df.to_parquet(path)
        return str(path.relative_to(root))
    except Exception:
        fallback = path.with_suffix(".pkl")
        df.to_pickle(fallback)
        return str(fallback.relative_to(root))


def _build_manifest(
    out_dir: Path,
    dataset_id: str,
    symbols: List[str],
    kinds: Iterable[str],
    start: Optional[str],
    end: Optional[str],
    timeframe: str,
    orderbook_limit: int,
    orderbook_samples: int,
    results: List[Dict],
) -> Dict:
    artifacts = sorted({r["artifact"] for r in results if r.get("artifact")})
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dataset_id": dataset_id,
        "symbols": symbols,
        "kinds": list(kinds),
        "start": start,
        "end": end,
        "timeframe": timeframe,
        "orderbook_limit": orderbook_limit,
        "orderbook_samples": orderbook_samples,
        "results": results,
        "artifacts": artifacts,
        "artifact_fingerprints": {name: _sha256_file(out_dir / name) for name in artifacts},
    }


def _indexed(rows: List[Dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df.dropna(subset=["timestamp"]).set_index("timestamp").sort_index()


def _safe_symbol(symbol: str) -> str:
    return symbol.replace("/", "_").replace(":", "_")


def _to_ms(value: str) -> int:
    return int(pd.Timestamp(value, tz="UTC").timestamp() * 1000)


def _timestamp_from_ms(value) -> Optional[pd.Timestamp]:
    if value in (None, ""):
        return None
    try:
        return pd.Timestamp(int(value), unit="ms", tz="UTC")
    except Exception:
        return None


def _now_utc() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC")


def _as_float(value) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _nested_float(data: Dict, keys: List[str]) -> Optional[float]:
    cur = data
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return _as_float(cur)


def _info_value(item: Dict, key: str):
    info = item.get("info") or {}
    return info.get(key)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
