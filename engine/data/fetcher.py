"""
data/fetcher.py
===============
OHLCV data fetching layer.

Primary source : ccxt → OKX (spot or swap/futures)
Fallback source : yfinance (spot only, ticker = "BTC-USD" style)

Data is cached locally as Parquet files to avoid redundant API calls.
"""

import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import ccxt
import pandas as pd

from config.settings import (
    BACKTEST_END,
    BACKTEST_GUARDS,
    BACKTEST_START,
    DATA_DIR,
    TIMEFRAME,
    TRADING_MODE,
)
from execution.broker import create_exchange

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stablecoins and tokens to exclude from dynamic universe
# ---------------------------------------------------------------------------
_STABLECOIN_BASES = {
    "USDC", "BUSD", "TUSD", "USDP", "DAI", "FDUSD", "USDD", "PYUSD",
    "EUR", "GBP", "AUD", "BRL", "TRY",
}

_EXCLUDE_BASES = _STABLECOIN_BASES | {
    "BTCDOM", "DEFI",  # index tokens, not real assets
    "XAU", "XAG", "PAXG",  # commodity-pegged, not crypto
}

# TradFi perps — exclude them
_TRADFI_BASES = {
    "MSTR", "TSLA", "CRCL", "AAPL", "AMZN", "GOOG", "MSFT", "META",
    "NVDA", "NFLX", "COIN", "SQ", "PYPL", "BABA", "PLTR",
}

# Historical symbol renames on OKX. Keep the backtest universe stable while
# transparently fetching the currently listed successor contracts.
_FUTURES_SYMBOL_ALIASES = {
    "MATIC/USDT": "POL/USDT",
    "MKR/USDT": "SKY/USDT",
    "FTM/USDT": "S/USDT",
    "1000SATS/USDT": "SATS/USDT",
}


# ---------------------------------------------------------------------------
# Dynamic universe discovery
# ---------------------------------------------------------------------------

def fetch_tradable_futures_symbols(
    min_daily_volume_usd: float = 5_000_000.0,
    max_symbols: int = 60,
    sandbox: bool = False,
) -> List[str]:
    """
    Fetch all active USDT-margined perpetual swaps from OKX,
    filter by 24h volume, exclude stablecoins and index tokens.

    Returns a list of ccxt symbols like ['BTC/USDT', 'ETH/USDT', ...],
    sorted by 24h volume descending, capped at max_symbols.
    """
    ex = _make_exchange("futures", sandbox=sandbox)
    tickers = ex.fetch_tickers()

    candidates = []
    for symbol, ticker in tickers.items():
        # Only USDT-margined perps
        if not symbol.endswith("/USDT") and not symbol.endswith("/USDT:USDT"):
            continue

        # Normalize symbol to ccxt format
        clean_sym = symbol.split(":")[0]  # "BTC/USDT:USDT" -> "BTC/USDT"
        base = clean_sym.split("/")[0]

        # Exclude stablecoins, index tokens, and TradFi perps
        if base in _EXCLUDE_BASES or base in _TRADFI_BASES:
            continue

        # Check market is active
        market = ex.markets.get(symbol)
        if market and not market.get("active", True):
            continue

        vol_24h = float(ticker.get("quoteVolume", 0) or 0)
        if vol_24h < min_daily_volume_usd:
            continue

        candidates.append((clean_sym, vol_24h))

    # Sort by volume descending, take top N
    candidates.sort(key=lambda x: x[1], reverse=True)
    symbols = [sym for sym, _ in candidates[:max_symbols]]

    logger.info(
        "Dynamic universe: %d symbols (from %d candidates, min vol=$%.0fM)",
        len(symbols), len(tickers), min_daily_volume_usd / 1e6,
    )
    return symbols


# ---------------------------------------------------------------------------
# Exchange factory
# ---------------------------------------------------------------------------

def _make_exchange(mode: str = "spot", sandbox: bool = False) -> ccxt.Exchange:
    """
    Return a ccxt.okx exchange for market data.

    Uses an UNAUTHENTICATED client for historical data (OHLCV is public).
    Authenticated client is used only when sandbox=True (demo account keys).
    """
    options = {"defaultType": "swap" if mode == "futures" else "spot"}
    if mode == "futures":
        # Limit market discovery to swaps so historical futures fetches don't
        # first enumerate SPOT/FUTURE/OPTION markets and fail noisily.
        options["fetchMarkets"] = {"types": ["swap"]}

    if sandbox:
        ex = create_exchange(mode=mode, sandbox=True)
    else:
        ex = ccxt.okx({"enableRateLimit": True, "options": options})
    ex.load_markets()
    return ex


def _resolve_symbol_alias(symbol: str, mode: str) -> str:
    if mode != "futures":
        return symbol
    return _FUTURES_SYMBOL_ALIASES.get(symbol, symbol)


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _cache_path(symbol: str, mode: str, timeframe: str, ext: str = "parquet") -> Path:
    safe = symbol.replace("/", "_")
    return DATA_DIR / f"{safe}_{mode}_{timeframe}.{ext}"


def _load_cache(symbol: str, mode: str, timeframe: str) -> Optional[pd.DataFrame]:
    parquet_path = _cache_path(symbol, mode, timeframe, "parquet")
    pickle_path = _cache_path(symbol, mode, timeframe, "pkl")

    if parquet_path.exists():
        try:
            df = pd.read_parquet(parquet_path)
            logger.debug("Cache hit: %s", parquet_path)
            return df
        except Exception as e:
            logger.warning("Parquet cache unreadable for %s (%s): %s", symbol, parquet_path.name, e)

    if pickle_path.exists():
        try:
            df = pd.read_pickle(pickle_path)
            logger.debug("Cache hit: %s", pickle_path)
            return df
        except Exception as e:
            logger.warning("Pickle cache unreadable for %s (%s): %s", symbol, pickle_path.name, e)

    return None


def _save_cache(df: pd.DataFrame, symbol: str, mode: str, timeframe: str) -> None:
    parquet_path = _cache_path(symbol, mode, timeframe, "parquet")
    pickle_path = _cache_path(symbol, mode, timeframe, "pkl")
    try:
        df.to_parquet(parquet_path)
        logger.debug("Cached: %s", parquet_path)
        return
    except Exception as e:
        logger.warning("Parquet cache write failed for %s: %s; falling back to pickle", symbol, e)

    try:
        df.to_pickle(pickle_path)
        logger.debug("Cached: %s", pickle_path)
    except Exception as e:
        logger.warning("Pickle cache write failed for %s: %s", symbol, e)


# ---------------------------------------------------------------------------
# Core fetch function
# ---------------------------------------------------------------------------

def fetch_ohlcv(
    symbol: str,
    start: str = BACKTEST_START,
    end: str = BACKTEST_END,
    mode: str = TRADING_MODE,
    timeframe: str = TIMEFRAME,
    use_cache: bool = True,
    sandbox: bool = False,
) -> pd.DataFrame:
    """
    Fetch daily OHLCV for *symbol* between *start* and *end*.

    Returns a DataFrame with columns [open, high, low, close, volume, funding_rate]
    indexed by UTC date (DatetimeIndex, daily frequency).

    Falls back to yfinance if ccxt fails (spot only).
    """
    cached = None
    if use_cache:
        cached = _load_cache(symbol, mode, timeframe)
        if cached is not None and not cached.empty:
            # Only trust the cache if it covers the requested end date.
            # Allow a small tolerance for missing trailing bars.
            tolerance = pd.Timedelta(days=2) if timeframe == "1d" else pd.Timedelta(hours=8)
            requested_end = _parse_bound(end, timeframe=timeframe, is_end=True)
            if cached.index.max() >= requested_end - tolerance:
                return _slice_dates(cached, start, end)
            logger.info(
                "Cache stale for %s: ends %s, need %s — re-fetching",
                symbol, cached.index.max().date(), requested_end.date(),
            )

    try:
        df = _fetch_ccxt(symbol, start, end, mode, timeframe=timeframe, sandbox=sandbox)
    except Exception as e:
        logger.warning("ccxt fetch failed for %s (%s): %s. Trying yfinance.", symbol, mode, e)
        try:
            df = _fetch_yfinance(symbol, start, end)
        except Exception:
            if cached is not None and not cached.empty:
                logger.warning(
                    "Falling back to stale cache for %s after live fetch failure; cache ends at %s",
                    symbol, cached.index.max(),
                )
                df = cached
            else:
                raise

    if use_cache and df is not None and not df.empty:
        _save_cache(df, symbol, mode, timeframe)

    df = _slice_dates(df, start, end)
    if "funding_rate" not in df.columns:
        df["funding_rate"] = 0.0
    return df


def _fetch_funding_rates(
    ex: ccxt.Exchange,
    symbol: str,
    since_ms: int,
    end_ms: int,
    timeframe: str,
) -> pd.Series:
    """Fetch historical funding rates for a perpetual futures symbol."""
    all_rates: List[Dict] = []
    cursor = since_ms
    try:
        while cursor < end_ms:
            rates = ex.fetch_funding_rate_history(symbol, since=cursor, limit=1000)
            if not rates:
                break
            all_rates.extend(rates)
            cursor = rates[-1]["timestamp"] + 1
            time.sleep(ex.rateLimit / 1000)
    except Exception as e:
        logger.warning("Could not fetch funding rates for %s: %s", symbol, e)
        return pd.Series(dtype=float)

    if not all_rates:
        return pd.Series(dtype=float)

    funding_df = pd.DataFrame(all_rates)
    funding_df["timestamp"] = pd.to_datetime(funding_df["timestamp"], unit="ms", utc=True)
    funding_df = funding_df.set_index("timestamp").sort_index()
    funding_df.index = _normalize_index_for_timeframe(funding_df.index, timeframe)
    rate_col = "fundingRate" if "fundingRate" in funding_df.columns else "rate"
    if rate_col not in funding_df.columns:
        return pd.Series(dtype=float)
    series = funding_df[rate_col].groupby(level=0).mean()
    return series.astype(float)


def _fetch_ccxt(
    symbol: str,
    start: str,
    end: str,
    mode: str,
    timeframe: str,
    sandbox: bool = False,
) -> pd.DataFrame:
    """Download OHLCV using ccxt with pagination."""
    ex = _make_exchange(mode, sandbox=sandbox)
    source_symbol = _resolve_symbol_alias(symbol, mode)

    # Normalize symbol for futures (BTC/USDT → BTC/USDT:USDT)
    fetch_symbol = source_symbol
    if mode == "futures" and ":" not in source_symbol:
        candidate = f"{source_symbol}:USDT"
        if candidate in ex.markets:
            fetch_symbol = candidate

    if source_symbol != symbol:
        logger.info("Using symbol alias for futures history: %s -> %s", symbol, source_symbol)

    start_ts = _parse_bound(start, timeframe=timeframe, is_end=False)
    end_ts = _parse_bound(end, timeframe=timeframe, is_end=True)
    since_ms = int(start_ts.timestamp() * 1000)
    end_ms = int(end_ts.timestamp() * 1000)
    funding_since_ms = since_ms

    all_bars: List[List] = []
    while True:
        bars = ex.fetch_ohlcv(fetch_symbol, timeframe=timeframe, since=since_ms, limit=1000)
        if not bars:
            break
        all_bars.extend(bars)
        last_ts = bars[-1][0]
        if last_ts >= end_ms:
            break
        since_ms = last_ts + 1
        time.sleep(ex.rateLimit / 1000)  # respect rate limit

    if not all_bars:
        raise ValueError(f"No OHLCV data returned for {symbol}")

    df = pd.DataFrame(all_bars, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.set_index("timestamp").sort_index()
    df.index = _normalize_index_for_timeframe(df.index, timeframe)
    df = df[~df.index.duplicated(keep="last")]

    if mode == "futures":
        funding = _fetch_funding_rates(ex, fetch_symbol, funding_since_ms, end_ms, timeframe)
        df["funding_rate"] = funding.reindex(df.index).fillna(0.0)
    else:
        df["funding_rate"] = 0.0
    return df.astype(float)


def _fetch_yfinance(symbol: str, start: str, end: str) -> pd.DataFrame:
    """Fallback: yfinance download (spot only). Converts 'BTC/USDT' → 'BTC-USD'."""
    try:
        import yfinance as yf
    except ImportError:
        raise ImportError("yfinance not installed. Run: pip install yfinance")

    # Map ccxt symbol to yfinance ticker
    base = symbol.split("/")[0]
    ticker_map = {
        "BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD",
        "BNB": "BNB-USD", "ADA": "ADA-USD", "AVAX": "AVAX-USD",
        "MATIC": "MATIC-USD", "DOT": "DOT-USD", "LINK": "LINK-USD",
        "UNI": "UNI-USD",
    }
    ticker = ticker_map.get(base, f"{base}-USD")

    raw = yf.download(ticker, start=start, end=end, auto_adjust=True, progress=False)
    if raw.empty:
        raise ValueError(f"yfinance returned no data for {ticker}")

    df = raw[["Open", "High", "Low", "Close", "Volume"]].copy()
    df.columns = ["open", "high", "low", "close", "volume"]
    df.index = pd.to_datetime(df.index, utc=True).normalize()
    df.index.name = "timestamp"
    df["funding_rate"] = 0.0
    return df.astype(float)


# ---------------------------------------------------------------------------
# Multi-symbol fetch
# ---------------------------------------------------------------------------

def fetch_universe(
    symbols: List[str],
    start: str = BACKTEST_START,
    end: str = BACKTEST_END,
    mode: str = TRADING_MODE,
    timeframe: str = TIMEFRAME,
    use_cache: bool = True,
    sandbox: bool = False,
) -> Dict[str, pd.DataFrame]:
    """
    Fetch OHLCV for each symbol in *symbols*.
    Returns a dict {symbol: DataFrame}.
    """
    data: Dict[str, pd.DataFrame] = {}
    for i, sym in enumerate(symbols):
        logger.info("Fetching %s [%s %s] (%d/%d)", sym, mode, timeframe, i + 1, len(symbols))
        try:
            df = fetch_ohlcv(
                sym,
                start=start,
                end=end,
                mode=mode,
                timeframe=timeframe,
                use_cache=use_cache,
                sandbox=sandbox,
            )
            data[sym] = df
        except Exception as exc:
            logger.error("Failed to fetch %s: %s", sym, exc)
        # Throttle between symbols to respect OKX rate limits
        if i < len(symbols) - 1:
            time.sleep(0.5)
    return data


def build_field_matrix(
    data: Dict[str, pd.DataFrame],
    field: str,
    max_ffill_days: Optional[int] = None,
) -> pd.DataFrame:
    """Stack an arbitrary field into a wide DataFrame."""
    matrix = {sym: df[field] for sym, df in data.items() if field in df.columns}
    frame = pd.DataFrame(matrix).sort_index()
    if max_ffill_days is not None and max_ffill_days > 0:
        frame = frame.ffill(limit=max_ffill_days)
    return frame


def build_close_matrix(data: Dict[str, pd.DataFrame], max_ffill_days: Optional[int] = None) -> pd.DataFrame:
    """Stack close prices into a wide DataFrame: index=date, columns=symbols."""
    if max_ffill_days is None:
        max_ffill_days = int(BACKTEST_GUARDS["max_ffill_days"])
    return build_field_matrix(data, "close", max_ffill_days=max_ffill_days)


def build_open_matrix(data: Dict[str, pd.DataFrame], max_ffill_days: Optional[int] = None) -> pd.DataFrame:
    """Stack open prices into a wide DataFrame: index=date, columns=symbols."""
    if max_ffill_days is None:
        max_ffill_days = 0
    return build_field_matrix(data, "open", max_ffill_days=max_ffill_days)


# ---------------------------------------------------------------------------
# Synthetic data (for quick-test / CI when exchange is unreachable)
# ---------------------------------------------------------------------------

def generate_synthetic_universe(
    symbols: List[str],
    start: str = BACKTEST_START,
    end: str = BACKTEST_END,
    seed: int = 42,
    timeframe: str = TIMEFRAME,
) -> Dict[str, pd.DataFrame]:
    """
    Generate realistic-ish synthetic OHLCV data via geometric Brownian motion.
    Used by --quick-test and CI pipelines.
    """
    import numpy as np

    rng = np.random.default_rng(seed)
    dates = pd.date_range(
        _parse_bound(start, timeframe=timeframe, is_end=False),
        _parse_bound(end, timeframe=timeframe, is_end=True),
        freq=timeframe,
        tz="UTC",
    )
    n = len(dates)

    # Rough starting prices (approximate 2020 levels)
    start_prices = {
        "BTC/USDT": 7_200, "ETH/USDT": 130, "SOL/USDT": 1.5,
        "BNB/USDT": 14,    "ADA/USDT": 0.04, "AVAX/USDT": 4.0,
        "MATIC/USDT": 0.02, "DOT/USDT": 5.0, "LINK/USDT": 2.5,
        "UNI/USDT": 1.0,
    }

    result: Dict[str, pd.DataFrame] = {}
    for sym in symbols:
        s0 = start_prices.get(sym, 10.0)
        periods_per_day = max(_bars_per_day(timeframe), 1)
        mu = 0.0005 / periods_per_day
        sigma = 0.035 / (periods_per_day ** 0.5)
        shocks = rng.normal(mu - 0.5 * sigma**2, sigma, n)
        log_prices = np.cumsum(np.insert(shocks, 0, np.log(s0)))
        close = np.exp(log_prices[1:])

        spread = rng.uniform(0.001, 0.003, n)
        high   = close * (1 + spread)
        low    = close * (1 - spread)
        open_  = close * (1 + rng.normal(0, 0.002, n))
        volume = rng.uniform(1e6, 1e8, n)

        df = pd.DataFrame(
            {
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "funding_rate": rng.normal(0.0, 0.0001, n),
            },
            index=dates[:n],
        )
        df.index.name = "timestamp"
        result[sym] = df.astype(float)

    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slice_dates(df: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    return df.loc[start:end]


def _parse_bound(value: str, timeframe: str, is_end: bool) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")

    if is_end and isinstance(value, str) and len(value) == 10 and timeframe != "1d":
        return ts + pd.Timedelta(days=1) - pd.Timedelta(milliseconds=1)
    return ts


def _normalize_index_for_timeframe(index: pd.DatetimeIndex, timeframe: str) -> pd.DatetimeIndex:
    if timeframe == "1d":
        return index.normalize()
    return index.floor(timeframe)


def _bars_per_day(timeframe: str) -> int:
    delta = pd.Timedelta("1D") / pd.Timedelta(timeframe)
    return max(int(delta), 1)
